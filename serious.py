# Preliminary setup for serious crowd-sourced model
# Exact setup should probably be in different repo
import os
import random
from typing import Any

import numpy as np
import torch.jit
from earl_pytorch import EARLPerceiver, ControlsPredictorDiscrete
from torch.nn import Sequential, Linear

import wandb
from rlgym.envs import Match
from rlgym.utils import ObsBuilder, TerminalCondition, RewardFunction, StateSetter
from rlgym.utils.common_values import ORANGE_TEAM, BOOST_LOCATIONS
from rlgym.utils.gamestates import PlayerData, GameState
from rlgym.utils.state_setters import StateWrapper
from rocket_learn.ppo import PPOAgent, PPO
from rocket_learn.rollout_generator.redis_rollout_generator import RedisRolloutGenerator


class SeriousObsBuilder(ObsBuilder):
    _boost_locations = np.array(BOOST_LOCATIONS)

    def __init__(self, n_players=6, tick_skip=8):
        super().__init__()
        self.n_players = n_players
        self.demo_timers = None
        self.boost_timers = None
        self.current_state = None
        self.tick_skip = tick_skip

    # With EARLPerceiver we can use relative coords+vel(+more?) for key/value tensor, might be smart
    def reset(self, initial_state: GameState):
        self.demo_timers = np.zeros(len(initial_state.players))
        self.boost_timers = np.zeros(len(initial_state.boost_pads))

    def build_obs(self, player: PlayerData, state: GameState, previous_action: np.ndarray) -> Any:
        # Lots of room for optimization
        invert = player.team_num == ORANGE_TEAM

        car_data = player.inverted_car_data if invert else player.car_data
        q = np.zeros(32)
        q[0] = 1  # is_main
        # q[1] = 0  # is_teammate
        # q[2] = 0  # is_opponent
        # q[3] = 0  # is_ball
        # q[4] = 0  # is_boost
        q[5:8] = car_data.position
        q[7:10] = car_data.linear_velocity
        q[10:13] = car_data.forward()
        q[13:16] = car_data.up()
        q[16:19] = car_data.angular_velocity
        q[20] = player.boost_amount
        q[21] = player.is_demoed
        q[22] = player.on_ground
        q[23] = player.has_flip
        q[24:] = previous_action

        # Consider including main player as well?
        kv = np.zeros((1 + self.n_players + len(state.boost_pads), 24))  # Ball, players, boosts

        ball = state.inverted_ball if invert else state.ball
        kv[0, 3] = 1
        kv[0, 5:8] = ball.position
        kv[0, 7:10] = ball.linear_velocity
        kv[0, 16:19] = ball.angular_velocity

        n = 1
        for i, other_player in enumerate(state.players):
            if other_player == player:
                continue
            if other_player.team_num == player.team_num:
                kv[n, 1] = 1  # is_teammate
            else:
                kv[n, 2] = 1  # is_opponent
            car_data = other_player.inverted_car_data if invert else other_player.car_data
            kv[n, 5:8] = car_data.position
            kv[n, 7:10] = car_data.linear_velocity
            kv[n, 10:13] = car_data.forward()
            kv[n, 13:16] = car_data.up()
            kv[n, 16:19] = car_data.angular_velocity
            kv[n, 20] = other_player.boost_amount
            kv[n, 21] = other_player.is_demoed  # Add demo timer?
            kv[n, 22] = other_player.on_ground
            kv[n, 23] = other_player.has_flip
            n += 1

        boost_pads = state.inverted_boost_pads if invert else state.boost_pads
        kv[n:, 5:8] = self._boost_locations
        kv[n:, 20] = 0.12 + 0.88 * (self._boost_locations[2] > 72)  # Boost amount
        kv[n:, 21] = boost_pads  # Add boost timer?

        mask = np.zeros(kv.shape[0])
        missing_players = self.n_players - len(state.players)
        assert missing_players > 0
        mask[-missing_players:] = 1

        kv[:, 4:10] -= q[4:10]  # Pos and vel are relative
        q = np.expand_dims(q, 0)  # Add extra dim at start for compatibility
        return q, kv, mask


class SeriousTerminalCondition(TerminalCondition):  # What a name
    # Probably just use simple goal and no touch terminals
    def reset(self, initial_state: GameState):
        pass

    def is_terminal(self, current_state: GameState) -> bool:
        pass


class SeriousRewardFunction(RewardFunction):
    # Something like DistributeRewards(EventReward(goal=4, shot=4, save=4, demo=4, touch=1))
    # but find a way to reduce dribble abuse
    # Also add std/max/min rewards to log so we can actually see progress
    def reset(self, initial_state: GameState):
        pass

    def get_reward(self, player: PlayerData, state: GameState, previous_action: np.ndarray) -> float:
        pass


class SeriousStateSetter(StateSetter):
    # Use anything other than DefaultState?
    # Random is useful at start since it has to actually learn where ball is (somewhat less necessary with relative obs)
    def reset(self, state_wrapper: StateWrapper):
        pass


def get_match():
    weights = (6, 3, 2)  # equal number of agents
    return Match(
        reward_function=SeriousRewardFunction(),
        terminal_conditions=SeriousTerminalCondition(),
        obs_builder=SeriousObsBuilder(),
        state_setter=SeriousStateSetter(),
        self_play=True,
        team_size=random.choices((1, 2, 3), weights)[0],  # Use mix of 1s, 2s and 3s?
    )


if __name__ == "__main__":
    wandb.login(key=os.environ["WANDB_KEY"])
    logger = wandb.init(project="rocket-learn", entity="rolv-arild")

    rollout_gen = RedisRolloutGenerator(password="rocket-learn", logger=logger, save_every=1)

    d = 256
    actor = torch.jit.trace(Sequential(Linear(d, d), ControlsPredictorDiscrete(d)), torch.zeros(1, 1, d))
    critic = torch.jit.trace(Sequential(Linear(d, d), Linear(d, 1)), torch.zeros(1, 1, d))
    shared = torch.jit.trace(EARLPerceiver(d, query_features=32, key_value_features=24), (torch.zeros(1, 1, 32),) * 3)

    agent = PPOAgent(actor=actor, critic=critic, shared=shared)

    lr = 1e-5
    alg = PPO(
        rollout_gen,
        agent,
        n_steps=1_000_000,
        batch_size=10_000,
        lr_critic=lr,
        lr_actor=lr,
        lr_shared=lr,
        epochs=10,
        logger=logger
    )

    log_dir = "E:\\log_directory\\"
    repo_dir = "E:\\repo_directory\\"

    alg.run()
