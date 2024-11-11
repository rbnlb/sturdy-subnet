# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 Syeam Bin Abdullah

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import json
from typing import Any, cast

import bittensor as bt
import gmpy2
import numpy as np
import numpy.typing as npt
import torch
from web3.constants import ADDRESS_ZERO

from sturdy.constants import QUERY_TIMEOUT, SIMILARITY_THRESHOLD
from sturdy.pools import POOL_TYPES, ChainBasedPoolModel, PoolFactory, check_allocations
from sturdy.protocol import AllocationsDict, AllocInfo
from sturdy.utils.ethmath import wei_div, wei_mul
from sturdy.validator.sql import get_db_connection, get_miner_responses, get_request_info


def get_response_times(uids: list[str], responses, timeout: float) -> dict[str, float]:
    """
    Returns a list of axons based on their response times.

    This function pairs each uid with its corresponding axon's response time.
    Lower response times are considered better.

    Args:
        uids (list[int]): list of unique identifiers for each axon.
        responses (list[Response]): list of Response objects corresponding to each axon.

    Returns:
        list[Tuple[int, float]]: A sorted list of tuples, where each tuple contains an axon's uid and its response time.

    Example:
        >>> get_sorted_response_times(
        ...     [1, 2, 3],
        ...     [
        ...         response1,
        ...         response2,
        ...         response3,
        ...     ],
        ... )
        [(2, 0.1), (1, 0.2), (3, 0.3)]
    """
    return {
        str(uids[idx]): (response.dendrite.process_time if response.dendrite.process_time is not None else timeout)
        for idx, response in enumerate(responses)
    }
    # Sorting in ascending order since lower process time is better


def format_allocations(
    allocations: AllocationsDict,
    assets_and_pools: dict,
) -> AllocationsDict:
    # TODO: better way to do this?
    if allocations is None:
        allocations = {}
    allocs = allocations.copy()
    pools: Any = assets_and_pools["pools"]

    # pad the allocations
    for contract_addr in pools:
        if contract_addr not in allocs:
            allocs[contract_addr] = 0

    # sort the allocations by contract address
    return {contract_addr: allocs[contract_addr] for contract_addr in sorted(allocs.keys())}


def normalize_squared(
    apys_and_allocations: AllocationsDict, z_threshold: float = 1.0, q: float = 0.75, epsilon: float = 1e-8
) -> torch.Tensor:
    raw_apys = {uid: apys_and_allocations[uid]["apy"] for uid in apys_and_allocations}

    # TODO: is there a better way to go about this?
    if len(raw_apys) <= 1:
        return torch.zeros(len(raw_apys))

    apys = torch.tensor(list(raw_apys.values()))

    squared = torch.pow(apys, 2)

    return (squared - squared.min()) / (squared.max() - squared.min() + epsilon)


def calculate_penalties(
    similarity_matrix: dict[str, dict[str, float]],
    axon_times: dict[str, float],
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> dict[str, int]:
    penalties = {miner: 0 for miner in similarity_matrix}

    for miner_a, similarities in similarity_matrix.items():
        for miner_b, similarity in similarities.items():
            if similarity <= similarity_threshold and axon_times[miner_a] <= axon_times[miner_b]:
                penalties[miner_b] += 1

    return penalties


def calculate_rewards_with_adjusted_penalties(miners, rewards_apy, penalties) -> torch.Tensor:
    rewards = torch.zeros(len(miners))
    max_penalty = max(penalties.values())
    if max_penalty == 0:
        return rewards_apy

    for idx, miner_id in enumerate(miners):
        # Calculate penalty adjustment
        penalty_factor = (max_penalty - penalties[miner_id]) / max_penalty

        # Calculate the final reward
        reward = rewards_apy[idx] * penalty_factor
        rewards[idx] = reward

    return rewards


def get_distance(alloc_a: npt.NDArray, alloc_b: npt.NDArray, total_assets: int) -> float:
    diff = alloc_a - alloc_b
    norm = gmpy2.sqrt(sum(x**2 for x in diff))
    return norm / gmpy2.sqrt(float(2 * total_assets**2))


def get_similarity_matrix(
    apys_and_allocations: dict[str, dict[str, AllocationsDict | int]],
    assets_and_pools: dict[str, dict[str, ChainBasedPoolModel] | int],
) -> dict[str, dict[str, float]]:
    """
    Calculates the similarity matrix for the allocation strategies of miners using normalized Euclidean distance.

    This function computes a similarity matrix based on the Euclidean distance between the allocation vectors of miners,
    normalized by the maximum possible distance in the given asset space. Each miner's allocation is compared with every
    other miner's allocation, resulting in a matrix where each element (i, j) represents the normalized Euclidean distance
    between the allocations of miner_i and miner_j.

    The similarity metric is scaled between 0 and 1, where 0 indicates identical allocations and 1 indicates the maximum
    possible distance between the allocation 'vectors'.

    Args:
        apys_and_allocations (dict[str, dict[str, Union[AllocationsDict, int]]]):
            A dictionary containing the APY and allocation strategies for each miner. The keys are miner identifiers,
            and the values are dictionaries with their respective allocations and APYs.
        assets_and_pools (dict[str, Union[AllocationsDict, int]]):
            A dictionary representing the assets available to the miner as well as the pools they can allocate to

    Returns:
        dict[str, dict[str, float]]:
            A nested dictionary where each key is a miner identifier, and the value is another dictionary containing the
            normalized Euclidean distances to every other miner. The distances are scaled between 0 and 1.
    """

    similarity_matrix = {}
    total_assets = cast(int, assets_and_pools["total_assets"])
    for miner_a, info_a in apys_and_allocations.items():
        _alloc_a = cast(AllocationsDict, info_a["allocations"])
        alloc_a = np.array(
            [gmpy2.mpz(x) for x in list(format_allocations(_alloc_a, assets_and_pools).values())],
        )
        similarity_matrix[miner_a] = {}
        for miner_b, info_b in apys_and_allocations.items():
            if miner_a != miner_b:
                _alloc_b = cast(AllocationsDict, info_b["allocations"])
                if _alloc_a is None or _alloc_b is None:
                    similarity_matrix[miner_a][miner_b] = float("inf")
                    continue
                alloc_b = np.array(
                    [gmpy2.mpz(x) for x in list(format_allocations(_alloc_b, assets_and_pools).values())],
                )
                similarity_matrix[miner_a][miner_b] = get_distance(alloc_a, alloc_b, total_assets)

    return similarity_matrix


def adjust_rewards_for_plagiarism(
    self,
    rewards_apy: torch.Tensor,
    apys_and_allocations: dict[str, dict[str, AllocationsDict | int]],
    assets_and_pools: dict[str, dict[str, ChainBasedPoolModel] | int],
    uids: list,
    axon_times: dict[str, float],
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> torch.Tensor:
    """
    Adjusts the annual percentage yield (APY) rewards for miners based on the similarity of their allocations
    to others and their arrival times, penalizing plagiarized or overly similar strategies.

    This function calculates the similarity between each pair of miners' allocation strategies and applies a penalty
    to those whose allocations are too similar to others, considering the order in which they arrived. Miners who
    arrived earlier with unique strategies are given preference, and those with similar strategies arriving later
    are penalized. The final APY rewards are adjusted accordingly.

    Args:
        rewards_apy (torch.Tensor): The initial APY rewards for the miners, before adjustments.
        apys_and_allocations (dict[str, dict[str, Union[AllocationsDict, int]]]):
            A dictionary containing APY values and allocation strategies for each miner. The keys are miner identifiers,
            and the values are dictionaries that include their allocations and APYs.
        assets_and_pools (dict[str, Union[dict[str, int], int]]):
            A dictionary representing the available assets and their corresponding pools.
        uids (List): A list of unique identifiers for the miners.
        axon_times (dict[str, float]): A dictionary that tracks the arrival times of each miner, with the keys being
            miner identifiers and the values being their arrival times. Earlier times are lower values.

    Returns:
        torch.Tensor: The adjusted APY rewards for the miners, accounting for penalties due to similarity with
        other miners' strategies and their arrival times.
    Notes:
        - This function relies on the helper functions `calculate_penalties` and `calculate_rewards_with_adjusted_penalties`
          which are defined separately.
        - The `format_allocations` function used in the similarity calculation converts the allocation dictionaries
          to a consistent format suitable for comparison.
    """
    # Step 1: Calculate pairwise similarity (e.g., using Euclidean distance)
    similarity_matrix = get_similarity_matrix(apys_and_allocations, assets_and_pools)

    # Step 2: Apply penalties considering axon times
    penalties = calculate_penalties(similarity_matrix, axon_times, similarity_threshold)
    self.similarity_penalties = penalties

    # Step 3: Calculate final rewards with adjusted penalties
    return calculate_rewards_with_adjusted_penalties(uids, rewards_apy, penalties)


def _get_rewards(
    self,
    apys_and_allocations: dict[str, dict[str, AllocationsDict | int]],
    assets_and_pools: dict[str, dict[str, ChainBasedPoolModel] | int],
    uids: list[str],
    axon_times: dict[str, float],
) -> torch.Tensor:
    """
    Rewards miner responses to request. This method returns a reward
    value for the miner, which is used to update the miner's score.

    Returns:
    - adjusted_rewards: The reward values for the miners.
    """

    rewards_apy = normalize_squared(apys_and_allocations).to(self.device)

    return adjust_rewards_for_plagiarism(self, rewards_apy, apys_and_allocations, assets_and_pools, uids, axon_times)


def generated_yield_pct(
    allocations: AllocationsDict, assets_and_pools: dict[str, dict[str, ChainBasedPoolModel] | int], extra_metadata: dict
) -> int:
    """
    Calculates immediate projected yields given intial assets and pools, pool history, and number of timesteps
    """

    # calculate projected yield
    initial_balance = cast(int, assets_and_pools["total_assets"])
    pools = cast(dict[str, ChainBasedPoolModel], assets_and_pools["pools"])
    total_yield = 0

    for contract_addr, pool in pools.items():
        allocation = allocations[contract_addr]
        match pool.pool_type:
            case POOL_TYPES.STURDY_SILO:
                last_share_price = extra_metadata[contract_addr]
                curr_share_price = pool._price_per_share
                pct_delta = float(curr_share_price - last_share_price) / float(last_share_price)
                total_yield += int(allocation * pct_delta)
            case T if T in (POOL_TYPES.AAVE_DEFAULT, POOL_TYPES.AAVE_TARGET):
                last_income = extra_metadata[contract_addr]
                curr_income = pool._normalized_income
                pct_delta = float(curr_income - last_income) / float(last_income)
                total_yield += int(allocation * pct_delta)
            case _:
                total_yield += 0

    return wei_div(total_yield, initial_balance)


def filter_allocations(
    self,
    query: int,  # noqa: ARG001
    uids: list[str],
    responses: list,
    assets_and_pools: dict[str, dict[str, ChainBasedPoolModel] | int],
) -> dict[str, AllocInfo]:
    """
    Returns a tensor of rewards for the given query and responses.

    Args:
    - query (int): The query sent to the miner.
    - responses (list[float]): A list of responses from the miner.

    Returns:
    - torch.Tensor: A tensor of rewards for the given query and responses.
    - allocs: miner allocations along with their respective yields
    """

    filtered_allocs = {}
    axon_times = get_response_times(uids=uids, responses=responses, timeout=QUERY_TIMEOUT)

    for response_idx, response in enumerate(responses):
        allocations = response.allocations

        # is the miner cheating w.r.t allocations?
        cheating = True
        try:
            cheating = not check_allocations(assets_and_pools, allocations)
        except Exception as e:
            bt.logging.error(e)  # type: ignore[]

        # score response very low if miner is cheating somehow or returns allocations with incorrect format
        if cheating:
            miner_uid = uids[response_idx]
            bt.logging.warning(f"CHEATER DETECTED | UID {miner_uid}")
            continue

        # used to filter out miners who timed out
        # TODO: should probably move some things around later down the road
        # TODO: cleaner way to do this?
        if response.allocations is not None or axon_times[uids[response_idx]] < QUERY_TIMEOUT:
            filtered_allocs[uids[response_idx]] = {
                "allocations": response.allocations,
            }

    curr_filtered_allocs = dict(sorted(filtered_allocs.items(), key=lambda item: int(item[0])))
    sorted_axon_times = dict(sorted(axon_times.items(), key=lambda item: item[1]))

    bt.logging.debug(f"sorted axon times:\n{sorted_axon_times}")

    self.sorted_axon_times = sorted_axon_times

    # Get all the reward results by iteratively calling your reward() function.
    return axon_times, curr_filtered_allocs


def get_rewards(self, active_allocation) -> tuple[list, dict]:
    # a dictionary, miner uids -> apy and allocations
    apys_and_allocations = {}
    miner_uids = []
    axon_times = {}

    # TODO: rename this here and in the database schema?
    request_uid = active_allocation["request_uid"]
    request_info = {}
    assets_and_pools = None
    miners = None

    with get_db_connection() as conn:
        # get assets and pools that are used to benchmark miner
        # we get the first row entry - we assume that it is the only response from the database
        try:
            request_info = get_request_info(conn, request_uid=request_uid)[0]
            assets_and_pools = json.loads(request_info["assets_and_pools"])
        except Exception:
            return ([], {})

        # obtain the miner responses for each request
        miners = get_miner_responses(conn, request_uid=request_uid)
        bt.logging.debug(f"filtered allocations: {miners}")

    # TODO: see if we can factor this into its own subroutine
    # if so, do the same with the same one in validator.py

    pools = assets_and_pools["pools"]
    new_pools = {}
    for uid, pool in pools.items():
        new_pool = PoolFactory.create_pool(
            pool_type=pool["pool_type"],
            web3_provider=self.w3,  # type: ignore[]
            user_address=(pool["user_address"]),  # TODO: is there a cleaner way to do this?
            contract_address=pool["contract_address"],
        )

        # sync pool
        new_pool.sync(self.w3)
        new_pools[uid] = new_pool

    assets_and_pools["pools"] = new_pools

    # TODO: this probably needs more work
    # TODO: would it be better here to use metrics i.e. liquidityIndex for aave pools?
    # calculate the "adjusted" yields of the allocations
    for miner in miners:
        allocations = json.loads(miner["allocation"])["allocations"]
        extra_metadata = json.loads(request_info["metadata"])
        miner_uid = miner["miner_uid"]
        miner_apy = generated_yield_pct(allocations, assets_and_pools, extra_metadata)
        miner_axon_time = miner["axon_time"]

        miner_uids.append(miner_uid)
        axon_times[miner_uid] = miner_axon_time
        apys_and_allocations[miner_uid] = {"apy": miner_apy, "allocations": allocations}

    # TODO: there may be a better way to go about this
    if len(miner_uids) < 1:
        return ([], {})

    # get rewards given the apys and allocations(s) with _get_rewards (???)
    return (miner_uids, _get_rewards(self, apys_and_allocations, assets_and_pools, miner_uids, axon_times))
