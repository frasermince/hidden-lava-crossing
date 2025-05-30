# docs and experiment results can be found at https://docs.cleanrl.dev/rl-algorithms/dqn/#dqn_jaxpy
import os
import random
import time
from dataclasses import dataclass
from enum import IntEnum

import flax
import flax.linen as nn
import gymnasium as gym
import jax
import jax.numpy as jnp
import minigrid
import numpy as np
import optax
import tyro
from flax.training.train_state import TrainState
from gymnasium.core import ActType, ObsType, WrapperObsType
from gymnasium import spaces
from gymnasium.envs.registration import register
from minigrid.core.constants import OBJECT_TO_IDX, TILE_PIXELS
from minigrid.core.grid import Grid
from minigrid.core.mission import MissionSpace
from minigrid.core.world_object import Box, Goal, Lava, Wall, WorldObj
from minigrid.minigrid_env import MiniGridEnv
from gymnasium.wrappers import TransformObservation
from minigrid.utils.rendering import (
    downsample,
    fill_coords,
    highlight_img,
    point_in_circle,
    point_in_rect,
    point_in_triangle,
    rotate_fn,
)
from tqdm import tqdm
from minigrid.wrappers import ImgObsWrapper
from stable_baselines3.common.buffers import DictReplayBuffer
from torch.utils.tensorboard import SummaryWriter

from networks_jax import CheapNet, GatedDQN, HeavyNet

from typing import Any, Iterable, SupportsFloat, TypeVar


class GrayscaleObservation(
    TransformObservation[WrapperObsType, ActType, ObsType],
    gym.utils.RecordConstructorArgs,
):
    """Converts an image observation computed by ``reset`` and ``step`` from RGB to Grayscale.

    The :attr:`keep_dim` will keep the channel dimension.

    A vector version of the wrapper exists :class:`gymnasium.wrappers.vector.GrayscaleObservation`.

    Example:
        >>> import gymnasium as gym
        >>> from gymnasium.wrappers import GrayscaleObservation
        >>> env = gym.make("CarRacing-v3")
        >>> env.observation_space.shape
        (96, 96, 3)
        >>> grayscale_env = GrayscaleObservation(env)
        >>> grayscale_env.observation_space.shape
        (96, 96)
        >>> grayscale_env = GrayscaleObservation(env, keep_dim=True)
        >>> grayscale_env.observation_space.shape
        (96, 96, 1)

    Change logs:
     * v0.15.0 - Initially added, originally called ``GrayScaleObservation``
     * v1.0.0 - Renamed to ``GrayscaleObservation``
    """

    def __init__(self, env: gym.Env[ObsType, ActType], keep_dim: bool = False):
        """Constructor for an RGB image based environments to make the image grayscale.

        Args:
            env: The environment to wrap
            keep_dim: If to keep the channel in the observation, if ``True``, ``obs.shape == 3`` else ``obs.shape == 2``
        """
        assert isinstance(env.observation_space.spaces["image"], spaces.Box)
        assert (
            len(env.observation_space.spaces["image"].shape) == 3
            and env.observation_space.spaces["image"].shape[-1] == 3
        )
        assert (
            np.all(env.observation_space.spaces["image"].low == 0)
            and np.all(env.observation_space.spaces["image"].high == 255)
            and env.observation_space.spaces["image"].dtype == np.uint8
        )
        gym.utils.RecordConstructorArgs.__init__(self, keep_dim=keep_dim)

        self.keep_dim: Final[bool] = keep_dim
        if keep_dim:
            new_observation_image_space = spaces.Box(
                low=0,
                high=255,
                shape=env.observation_space.spaces["image"].shape[:2] + (1,),
                dtype=np.uint8,
            )
            new_observation_space = env.observation_space
            new_observation_space.spaces["image"] = new_observation_image_space
            TransformObservation.__init__(
                self,
                env=env,
                func=lambda obs: {
                    **obs,
                    "image": np.expand_dims(
                        np.sum(
                            255
                            - np.multiply(
                                obs["image"], np.array([0.2125, 0.7154, 0.0721])
                            ),
                            axis=-1,
                        ).astype(np.uint8),
                        axis=-1,
                    ),
                },
                observation_space=new_observation_space,
            )
        else:
            new_observation_space = env.observation_space
            new_observation_space.spaces["image"] = spaces.Box(
                low=0,
                high=255,
                shape=env.observation_space.spaces["image"].shape[:2],
                dtype=np.uint8,
            )
            TransformObservation.__init__(
                self,
                env=env,
                func=lambda obs: {
                    **obs,
                    "image": np.sum(
                        255
                        - np.multiply(obs["image"], np.array([0.2125, 0.7154, 0.0721])),
                        axis=-1,
                    ).astype(np.uint8),
                },
                observation_space=new_observation_space,
            )


class DirectionlessGrid(Grid):
    def __init__(self, *args, **kwargs):
        self.invisible_goal = kwargs.pop("invisible_goal", False)
        super().__init__(*args, **kwargs)

    @classmethod
    def render_tile(
        cls,
        obj: WorldObj | None,
        agent_dir: int | None = None,
        highlight: bool = False,
        tile_size: int = TILE_PIXELS,
        subdivs: int = 3,
    ) -> np.ndarray:
        """
        Render a tile and cache the result
        """

        # Hash map lookup key for the cache
        key: tuple[Any, ...] = (agent_dir, highlight, tile_size)
        key = obj.encode() + key if obj else key

        if key in cls.tile_cache:
            return cls.tile_cache[key]

        img = np.zeros(
            shape=(tile_size * subdivs, tile_size * subdivs, 3), dtype=np.uint8
        )

        # Draw the grid lines (top and left edges)
        fill_coords(img, point_in_rect(0, 0.031, 0, 1), (100, 100, 100))
        fill_coords(img, point_in_rect(0, 1, 0, 0.031), (100, 100, 100))

        if obj is not None:
            obj.render(img)

        # Overlay the agent on top
        if agent_dir is not None:
            tri_fn = point_in_circle(
                0.5,
                0.5,
                0.3,
            )

            # Rotate the agent based on its direction
            # tri_fn = rotate_fn(tri_fn, cx=0.5, cy=0.5, theta=0.5 * math.pi * agent_dir)
            fill_coords(img, tri_fn, (255, 0, 0))

        # Highlight the cell if needed
        if highlight:
            highlight_img(img)

        # Downsample the image to perform supersampling/anti-aliasing
        img = downsample(img, subdivs)

        # Cache the rendered tile
        cls.tile_cache[key] = img

        return img

    def render(
        self,
        tile_size: int,
        agent_pos: tuple[int, int],
        agent_dir: int | None = None,
        highlight_mask: np.ndarray | None = None,
    ) -> np.ndarray:
        """
        Render this grid at a given scale
        :param r: target renderer object
        :param tile_size: tile size in pixels
        """

        if highlight_mask is None:
            highlight_mask = np.zeros(shape=(self.width, self.height), dtype=bool)

        # Compute the total grid size
        width_px = self.width * tile_size
        height_px = self.height * tile_size

        img = np.zeros(shape=(height_px, width_px, 3), dtype=np.uint8)

        # Render the grid
        for j in range(0, self.height):
            for i in range(0, self.width):
                cell = self.get(i, j)

                agent_here = np.array_equal(agent_pos, (i, j))
                assert highlight_mask is not None

                if (
                    isinstance(cell, Goal)
                    and cell.color == "green"
                    and self.invisible_goal
                ):
                    cell = None
                if isinstance(cell, Lava) and self.invisible_goal:
                    cell = None
                tile_img = DirectionlessGrid.render_tile(
                    cell,
                    agent_dir=agent_dir if agent_here else None,
                    highlight=highlight_mask[i, j],
                    tile_size=tile_size,
                )

                ymin = j * tile_size
                ymax = (j + 1) * tile_size
                xmin = i * tile_size
                xmax = (i + 1) * tile_size
                img[ymin:ymax, xmin:xmax, :] = tile_img

        return img


class ImgObsPositionWrapper(gym.ObservationWrapper):
    """
    Use the image as the only observation output, no language/mission.

    Example:
        >>> import gymnasium as gym
        >>> from minigrid.wrappers import ImgObsWrapper
        >>> env = gym.make("MiniGrid-Empty-5x5-v0")
        >>> obs, _ = env.reset()
        >>> obs.keys()
        dict_keys(['image', 'direction', 'mission'])
        >>> env = ImgObsWrapper(env)
        >>> obs, _ = env.reset()
        >>> obs.shape
        (7, 7, 3)
    """

    def __init__(self, env):
        """A wrapper that makes image the only observation.

        Args:
            env: The environment to apply the wrapper
        """
        super().__init__(env)
        self.observation_space = env.observation_space.spaces["image"]

    def observation(self, obs):
        return obs["image"], obs


class Actions(IntEnum):
    left = 0
    forward = 1
    right = 2
    backward = 3


class TMaze(MiniGridEnv):
    """
    ## Description

    Depending on the `obstacle_type` parameter:
    - `Lava` - The agent has to reach the green goal square on the other corner
        of the room while avoiding rivers of deadly lava which terminate the
        episode in failure. Each lava stream runs across the room either
        horizontally or vertically, and has a single crossing point which can be
        safely used; Luckily, a path to the goal is guaranteed to exist. This
        environment is useful for studying safety and safe exploration.
    - otherwise - Similar to the `LavaCrossing` environment, the agent has to
        reach the green goal square on the other corner of the room, however
        lava is replaced by walls. This MDP is therefore much easier and maybe
        useful for quickly testing your algorithms.

    ## Mission Space
    Depending on the `obstacle_type` parameter:
    - `Lava` - "avoid the lava and get to the green goal square"
    - otherwise - "find the opening and get to the green goal square"

    ## Action Space

    | Num | Name         | Action        |
    |-----|--------------|---------------|
    | 0   | left         | Move left     |
    | 1   | forward      | Move forward  |
    | 2   | right        | Move right    |
    | 3   | backward     | Move backward |

    ## Observation Encoding

    - Each tile is encoded as a 3 dimensional tuple:
        `(OBJECT_IDX, COLOR_IDX, STATE)`
    - `OBJECT_TO_IDX` and `COLOR_TO_IDX` mapping can be found in
        [minigrid/core/constants.py](minigrid/core/constants.py)
    - `STATE` refers to the door state with 0=open, 1=closed and 2=locked

    ## Rewards

    A reward of '1 - 0.9 * (step_count / max_steps)' is given for success, and '0' for failure.

    ## Termination

    The episode ends if any one of the following conditions is met:

    1. The agent reaches the goal.
    2. The agent falls into lava.
    3. Timeout (see `max_steps`).

    ## Registered Configurations

    S: size of the map SxS.
    N: number of valid crossings across lava or walls from the starting position
    to the goal

    - `Lava` :
        - `MiniGrid-LavaCrossingS9N1-v0`
        - `MiniGrid-LavaCrossingS9N2-v0`
        - `MiniGrid-LavaCrossingS9N3-v0`
        - `MiniGrid-LavaCrossingS11N5-v0`

    - otherwise :
        - `MiniGrid-SimpleCrossingS9N1-v0`
        - `MiniGrid-SimpleCrossingS9N2-v0`
        - `MiniGrid-SimpleCrossingS9N3-v0`
        - `MiniGrid-SimpleCrossingS11N5-v0`

    """

    def __init__(
        self,
        size=9,
        max_steps: int | None = None,
        **kwargs,
    ):
        # self.num_crossings = num_crossings
        # self.obstacle_type = obstacle_type
        self.goal_position = None
        self.path_episode_threshold = 2000
        self.num_episodes = 0

        mission_space = MissionSpace(mission_func=self._gen_mission)

        if max_steps is None:
            max_steps = 2 * size**2

        self.invisible_goal = kwargs.pop("invisible_goal", False)
        super().__init__(
            mission_space=mission_space,
            grid_size=size,
            see_through_walls=False,  # Set this to True for maximum speed
            max_steps=max_steps,
            **kwargs,
        )
        self.actions = Actions
        image_observation_space = gym.spaces.Box(
            low=0,
            high=255,
            shape=(self.agent_view_size, self.agent_view_size, 3),
            dtype="uint8",
        )
        self.action_space = gym.spaces.Discrete(4)
        self.observation_space = gym.spaces.Dict(
            {
                "image": image_observation_space,
                "direction": gym.spaces.Discrete(4),
                "mission": mission_space,
                "position": gym.spaces.Box(
                    low=0,
                    high=size - 1,
                    shape=(2,),
                    dtype="int64",
                ),
                "arrow": gym.spaces.Box(
                    low=-1.0,
                    high=1.0,
                    shape=(self.action_space.n,),
                    dtype="float32",
                ),
            }
        )

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        self.num_episodes += 1
        return super().reset(seed=seed, options=options)

    @staticmethod
    def _gen_mission():
        return "get to the green goal square"

    def _gen_grid(self, width, height):
        assert width % 2 == 1 and height % 2 == 1  # odd size

        # Create an empty grid
        self.grid = DirectionlessGrid(width, height, invisible_goal=self.invisible_goal)

        # Generate the surrounding walls
        self.grid.wall_rect(0, 0, width, height)
        self.grid.vert_wall(width // 2 - 1, 2, height - 3)
        self.grid.vert_wall(width // 2 + 1, 2, height - 3)
        self.grid.horz_wall(0, 0, width)
        self.grid.horz_wall(0, 2, width // 2)
        self.grid.horz_wall(width // 2 + 1, 2, width // 2)
        # self.grid.horz_wall(0, height - 1, width)
        # Place the agent in the top-left corner
        self.agent_pos = np.array((width // 2, height - 2))
        self.agent_dir = 3

        # Either place a goal square in the top-left or top-right corner
        goal_positions = [(1, 1), (width - 2, 1)]
        goal_choice = self.np_random.integers(2)
        lava_choice = 1 - goal_choice
        self.goal_position = goal_positions[goal_choice]
        self.lava_position = goal_positions[lava_choice]
        # print(self.goal_position)
        # Alternative: self.goal_position = self.np_random.choice(goal_positions, size=1)[0]

        self.arrow_q_values = np.zeros(self.action_space.n)
        self.arrow_q_values[
            int(np.array_equal(self.goal_position, np.array((width - 2, 1))))
        ] = 1.0
        self.put_obj(Goal(), *self.goal_position)
        self.put_obj(Lava(), *self.lava_position)
        # Place a green box in a random valid position
        # while True:
        #     box_pos = (
        #         self.np_random.integers(1, width - 1),
        #         self.np_random.integers(1, height - 1),
        #     )
        #     # Don't place box on agent start or goal positions
        #     if (
        #         box_pos != (1, 1)
        #         and box_pos != (width - 2, height - 2)
        #         and self.grid.get(*box_pos) is None
        #     ):
        #         green_box = Box("blue")
        #         self.put_obj(green_box, *box_pos)
        #         break

        # # Place obstacles (lava or walls)
        # v, h = object(), object()  # singleton `vertical` and `horizontal` objects

        # # Lava rivers or walls specified by direction and position in grid
        # rivers = [(v, i) for i in range(2, height - 2, 2)]
        # rivers += [(h, j) for j in range(2, width - 2, 2)]
        # self.np_random.shuffle(rivers)
        # rivers = rivers[: self.num_crossings]  # sample random rivers
        # rivers_v = sorted(pos for direction, pos in rivers if direction is v)
        # rivers_h = sorted(pos for direction, pos in rivers if direction is h)
        # obstacle_pos = itt.chain(
        #     itt.product(range(1, width - 1), rivers_h),
        #     itt.product(rivers_v, range(1, height - 1)),
        # )
        # for i, j in obstacle_pos:
        #     self.put_obj(self.obstacle_type(), i, j)

        # Sample path to goal
        # path = [h] * len(rivers_v) + [v] * len(rivers_h)
        # self.np_random.shuffle(path)

        # Create openings
        # limits_v = [0] + rivers_v + [height - 1]
        # limits_h = [0] + rivers_h + [width - 1]
        # room_i, room_j = 0, 0
        # for direction in path:
        #     if direction is h:
        #         i = limits_v[room_i + 1]
        #         j = self.np_random.choice(
        #             range(limits_h[room_j] + 1, limits_h[room_j + 1])
        #         )
        #         room_i += 1
        #     elif direction is v:
        #         i = self.np_random.choice(
        #             range(limits_v[room_i] + 1, limits_v[room_i + 1])
        #         )
        #         j = limits_h[room_j + 1]
        #         room_j += 1
        #     else:
        #         assert False
        #     self.grid.set(i, j, None)

        self.mission = "get to the green goal square"

    def gen_obs(self):
        """
        Generate the agent's view (partially observable, low-resolution encoding)
        """
        grey_box = Goal("grey")
        left_box = (self.width * 3 // 4 - 1, self.height * 2 // 3 - 1)
        right_box = (self.width * 3 // 4 + 1, self.height * 2 // 3 - 1)
        up_box = (self.width * 3 // 4, self.height * 2 // 3 - 2)
        down_box = (self.width * 3 // 4, self.height * 2 // 3)
        box_locations = [left_box, right_box, up_box, down_box]
        for box in box_locations:
            self.grid.set(box[0], box[1], None)

        goal_is_right = int(
            np.array_equal(self.goal_position, np.array((self.width - 2, 1)))
        )
        # if self.agent_pos[0] == (self.height - 1) // 2 and self.agent_pos[1] == 1:

        # else:
        #     maybe_goal_corner = np.random.randint(0, 2)

        if self.agent_pos[1] == 1:
            w, h = right_box if goal_is_right else left_box
        else:
            w, h = up_box
        if self.num_episodes > self.path_episode_threshold:
            self.put_obj(grey_box, w, h)
        else:
            # Randomly decide whether to place box at selected location
            for box in box_locations:
                if self.np_random.random() < 0.5:
                    self.put_obj(grey_box, box[0], box[1])
        grid, vis_mask = self.gen_obs_grid()

        # Encode the partially observable view into a numpy array
        image = grid.encode(vis_mask)

        # Observations are dictionaries containing:
        # - an image (partially observable view of the environment)
        # - the agent's direction/orientation (acting as a compass)
        # - a textual mission string (instructions for the agent)

        # print(self.agent_pos)
        maybe_random_arrow = self.np_random.uniform(
            low=-1.0, high=1.0, size=self.action_space.n
        )
        if self.agent_pos[0] == (self.height - 1) // 2 and self.agent_pos[1] == 1:
            maybe_random_arrow = self.arrow_q_values

        obs = {
            "image": image,
            "direction": self.agent_dir,
            "mission": self.mission,
            "position": self.agent_pos,
            "arrow": maybe_random_arrow,
        }
        return obs

    # Customized to remove highlight mask
    def get_full_render(self, highlight, tile_size):
        """
        Render a non-paratial observation for visualization
        """
        # Compute which cells are visible to the agent
        _, vis_mask = self.gen_obs_grid()

        # Compute the world coordinates of the bottom-left corner
        # of the agent's view area
        f_vec = self.dir_vec
        r_vec = self.right_vec
        top_left = (
            self.agent_pos
            + f_vec * (self.agent_view_size - 1)
            - r_vec * (self.agent_view_size // 2)
        )

        # For each cell in the visibility mask
        for vis_j in range(0, self.agent_view_size):
            for vis_i in range(0, self.agent_view_size):
                # If this cell is not visible, don't highlight it
                if not vis_mask[vis_i, vis_j]:
                    continue

                # Compute the world coordinates of this cell
                abs_i, abs_j = top_left - (f_vec * vis_j) + (r_vec * vis_i)

                if abs_i < 0 or abs_i >= self.width:
                    continue
                if abs_j < 0 or abs_j >= self.height:
                    continue

        # Render the whole grid
        img = self.grid.render(
            tile_size, self.agent_pos, self.agent_dir, highlight_mask=None
        )

        return img

    def step(
        self, action: ActType
    ) -> tuple[ObsType, SupportsFloat, bool, bool, dict[str, Any]]:
        self.step_count += 1

        reward = 0
        terminated = False
        truncated = False

        # Move left
        if action == self.actions.left:
            fwd_pos = np.array((self.agent_pos[0] - 1, self.agent_pos[1]))
            fwd_cell = self.grid.get(*fwd_pos)
            if fwd_cell is None or fwd_cell.can_overlap():
                self.agent_pos = tuple(fwd_pos)
            if fwd_cell is not None and fwd_cell.type == "goal":
                terminated = True
                reward = self._reward()
            if fwd_cell is not None and fwd_cell.type == "lava":
                terminated = True

        # Move right
        elif action == self.actions.right:
            fwd_pos = np.array((self.agent_pos[0] + 1, self.agent_pos[1]))
            fwd_cell = self.grid.get(*fwd_pos)
            if fwd_cell is None or fwd_cell.can_overlap():
                self.agent_pos = tuple(fwd_pos)
            if fwd_cell is not None and fwd_cell.type == "goal":
                terminated = True
                reward = self._reward()
            if fwd_cell is not None and fwd_cell.type == "lava":
                terminated = True

        # Move forward
        elif action == self.actions.forward:
            fwd_pos = np.array((self.agent_pos[0], self.agent_pos[1] - 1))
            fwd_cell = self.grid.get(*fwd_pos)
            if fwd_cell is None or fwd_cell.can_overlap():
                self.agent_pos = tuple(fwd_pos)
            if fwd_cell is not None and fwd_cell.type == "goal":
                terminated = True
                reward = self._reward()
            if fwd_cell is not None and fwd_cell.type == "lava":
                terminated = True

        # Move backward
        elif action == self.actions.backward:
            fwd_pos = np.array((self.agent_pos[0], self.agent_pos[1] + 1))
            fwd_cell = self.grid.get(*fwd_pos)
            if fwd_cell is None or fwd_cell.can_overlap():
                self.agent_pos = tuple(fwd_pos)
            if fwd_cell is not None and fwd_cell.type == "goal":
                terminated = True
                reward = self._reward()
            if fwd_cell is not None and fwd_cell.type == "lava":
                terminated = True

        # Done action (not used by default)
        elif action == self.actions.done:
            pass

        else:
            raise ValueError(f"Unknown action: {action}")

        if self.step_count >= self.max_steps:
            truncated = True

        if self.render_mode == "human":
            self.render()

        obs = self.gen_obs()

        return obs, reward, terminated, truncated, {}


# With this env reward would be
# 1 - 0.9 * (STEPS / MAX_STEPS)
# Optimal reward would be:
# 1 - 0.9 * (12 / 484) = 0.9776859504
register(
    id="MiniGrid-TMaze-v0-custom",
    entry_point="main_dqn:TMaze",
    kwargs={"size": 11},
)


@dataclass
class Args:
    exp_name: str = os.path.basename(__file__)[: -len(".py")]
    """the name of this experiment"""
    seed: int = 1
    """seed of the experiment"""
    track: bool = True
    """if toggled, this experiment will be tracked with Weights and Biases"""
    wandb_project_name: str = "TMaze"
    """the wandb's project name"""
    wandb_entity: str = "frasermince"
    """the entity (team) of wandb's project"""
    capture_video: bool = True
    """whether to capture videos of the agent performances (check out `videos` folder)"""
    save_model: bool = False
    """whether to save model into the `runs/{run_name}` folder"""
    upload_model: bool = False
    """whether to upload the saved model to huggingface"""
    hf_entity: str = ""
    """the user or org name of the model repository from the Hugging Face Hub"""
    experiment_description: str = "seen-goal"

    # Algorithm specific arguments
    env_id: str = "MiniGrid-TMaze-v0-custom"
    """the id of the environment"""
    total_timesteps: int = 500000
    """total timesteps of the experiments"""
    learning_rate: float = 5e-5
    """the learning rate of the optimizer"""
    invisible_goal: bool = False
    num_envs: int = 1
    """the number of parallel game environments"""
    buffer_size: int = 10000
    """the replay memory buffer size"""
    gamma: float = 0.99
    """the discount factor gamma"""
    tau: float = 1.0
    """the target network update rate"""
    target_network_frequency: int = 500
    """the timesteps it takes to update the target network"""
    batch_size: int = 128
    """the batch size of sample from the reply memory"""
    start_e: float = 1
    """the starting epsilon for exploration"""
    end_e: float = 0.05
    """the ending epsilon for exploration"""
    exploration_fraction: float = 0.5
    """the fraction of `total-timesteps` it takes from start-e to go end-e"""
    learning_starts: int = 10000
    """timestep to start learning"""
    train_frequency: int = 10
    """the frequency of training"""


def make_env(env_id, seed, idx, capture_video, run_name, invisible_goal):
    def thunk():
        if capture_video and idx == 1:
            env = gym.make(
                env_id, render_mode="rgb_array", invisible_goal=invisible_goal
            )
            env = minigrid.wrappers.RGBImgObsWrapper(env)
            env = GrayscaleObservation(env)
            env = gym.wrappers.RecordVideo(
                env,
                f"videos/{run_name}",
            )
        else:
            env = gym.make(env_id)
            env = minigrid.wrappers.RGBImgObsWrapper(env)
            env = GrayscaleObservation(env)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env = gym.wrappers.Autoreset(env)
        env.action_space.seed(seed)

        return env

    return thunk


# ALGO LOGIC: initialize agent here:
class QNetwork(nn.Module):
    action_dim: int

    @nn.compact
    def __call__(self, x: jnp.ndarray):
        x = nn.Dense(120)(x)
        x = nn.relu(x)
        x = nn.Dense(84)(x)
        x = nn.relu(x)
        x = nn.Dense(self.action_dim)(x)
        return x


class TrainState(TrainState):
    target_params: flax.core.FrozenDict


def linear_schedule(start_e: float, end_e: float, duration: int, t: int):
    slope = (end_e - start_e) / duration
    return max(slope * t + start_e, end_e)


if __name__ == "__main__":
    import stable_baselines3 as sb3

    if sb3.__version__ < "2.0":
        raise ValueError(
            """Ongoing migration: run the following command to install the new dependencies:

poetry run pip install "stable_baselines3==2.0.0a1"
"""
        )
    args = tyro.cli(Args)
    assert args.num_envs == 1, "vectorized envs are not supported at the moment"
    run_name = f"{args.env_id}__{args.exp_name}__seed_{args.seed}__{int(time.time())}__{args.experiment_description}__learning_rate_{args.learning_rate}"
    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s"
        % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    key = jax.random.PRNGKey(args.seed)
    key, q_key = jax.random.split(key, 2)

    # env setup
    envs = make_env(
        args.env_id, args.seed + 1, 1, args.capture_video, run_name, args.invisible_goal
    )()
    assert isinstance(
        envs.action_space, gym.spaces.Discrete
    ), "only discrete action space is supported"

    obs, _ = envs.reset(seed=args.seed)
    import matplotlib.pyplot as plt

    plt.imshow(obs["image"], cmap="gray")
    plt.savefig("obs_image.png")
    plt.close()
    q_network = HeavyNet(
        # obs_shape=envs.observation_space.shape,
        action_dim=envs.action_space.n,
    )
    q_state = TrainState.create(
        apply_fn=q_network.apply,
        params=q_network.init(
            q_key,
            jnp.expand_dims(jnp.array(obs["image"]), 0),
            jnp.expand_dims(jnp.array([obs["arrow"]]), 0),
        ),
        target_params=q_network.init(
            q_key,
            jnp.expand_dims(jnp.array(obs["image"]), 0),
            jnp.expand_dims(jnp.array([obs["arrow"]]), 0),
        ),
        tx=optax.adam(learning_rate=args.learning_rate),
    )
    print(
        "params",
        sum(x.size for x in jax.tree.leaves(q_state.params)),
        "target_params",
        sum(x.size for x in jax.tree.leaves(q_state.target_params)),
    )

    q_network.apply = jax.jit(q_network.apply)
    # This step is not necessary as init called on same observation and key will always lead to same initializations
    q_state = q_state.replace(
        target_params=optax.incremental_update(q_state.params, q_state.target_params, 1)
    )

    rb = DictReplayBuffer(
        args.buffer_size,
        gym.spaces.Dict(
            {
                "image": envs.observation_space["image"],
                "arrow": envs.observation_space["arrow"],
            }
        ),
        envs.action_space,
        "cpu",
        handle_timeout_termination=False,
    )

    @jax.jit
    def update(
        q_state,
        observations,
        arrows,
        actions,
        next_observations,
        next_arrows,
        rewards,
        dones,
    ):
        q_next_target = q_network.apply(
            q_state.target_params, next_observations, next_arrows
        )  # (batch_size, num_actions)
        q_next_target = jnp.max(q_next_target, axis=-1)  # (batch_size,)
        next_q_value = rewards + (1 - dones) * args.gamma * q_next_target

        def mse_loss(params):
            q_pred = q_network.apply(
                params, observations, arrows
            )  # (batch_size, num_actions)
            q_pred = q_pred[
                jnp.arange(q_pred.shape[0]), actions.squeeze()
            ]  # (batch_size,)
            return ((q_pred - next_q_value) ** 2).mean(), q_pred

        (loss_value, q_pred), grads = jax.value_and_grad(mse_loss, has_aux=True)(
            q_state.params
        )
        q_state = q_state.apply_gradients(grads=grads)
        return loss_value, q_pred, q_state

    start_time = time.time()

    # TRY NOT TO MODIFY: start the game
    obs, _ = envs.reset(seed=args.seed)

    terminations = np.array([False])
    truncations = np.array([False])
    seed = args.seed
    for global_step in tqdm(range(args.total_timesteps)):
        # ALGO LOGIC: put action logic here
        epsilon = linear_schedule(
            args.start_e,
            args.end_e,
            args.exploration_fraction * args.total_timesteps,
            global_step,
        )
        if random.random() < epsilon:
            actions = np.array([envs.action_space.sample() for _ in range(1)])
        else:
            q_values = q_network.apply(
                q_state.params,
                jnp.expand_dims(jnp.array(obs["image"]), 0),
                jnp.expand_dims(jnp.array([obs["arrow"]]), 0),
            )
            actions = q_values.argmax(axis=-1)
            actions = jax.device_get(actions)

        # TRY NOT TO MODIFY: execute the game and log data.
        if terminations or truncations:
            # key, new_key = jax.random.split(key)
            # seed = int(jax.random.randint(new_key, (), 0, 2**30))
            # rng = np.random.default_rng(np.asarray(new_key))
            obs, _ = envs.reset()
            terminations = np.array([False])
            truncations = np.array([False])
        else:
            next_obs, rewards, terminations, truncations, infos = envs.step(actions)
        # next_obs = np.expand_dims(obs["image"], axis=0)
        terminations = np.expand_dims(terminations, axis=0)
        truncations = np.expand_dims(truncations, axis=0)

        # TRY NOT TO MODIFY: record rewards for plotting purposes
        if np.any(truncations) or np.any(terminations):
            writer.add_scalar(
                "charts/episodic_return", infos["episode"]["r"], global_step
            )
            writer.add_scalar(
                "charts/episodic_length", infos["episode"]["l"], global_step
            )
            # if "final_info" in infos:
            #     for info in infos["final_info"]:
            #         if info and "episode" in info:
            #         print(
            #             f"global_step={global_step}, episodic_return={info['episode']['r']}"
            #         )
            #         writer.add_scalar(
            #             "charts/episodic_return", info["episode"]["r"], global_step
            #         )
            #         writer.add_scalar(
            #             "charts/episodic_length", info["episode"]["l"], global_step
            #         )

        # TRY NOT TO MODIFY: save data to reply buffer; handle `final_observation`
        real_next_obs = next_obs.copy()
        # for idx, trunc in enumerate(truncations):
        #     if trunc:
        #         import pdb

        #         pdb.set_trace()
        #         real_next_obs[idx] = infos["final_observation"][idx]
        rb.add(
            {"image": obs["image"], "arrow": obs["arrow"]},
            {"image": real_next_obs["image"], "arrow": real_next_obs["arrow"]},
            actions,
            rewards,
            terminations,
            infos,
        )
        # TRY NOT TO MODIFY: CRUCIAL step easy to overlook
        obs = next_obs

        # ALGO LOGIC: training.
        if global_step > args.learning_starts:
            if global_step % args.train_frequency == 0:
                data = rb.sample(args.batch_size)
                # perform a gradient-descent step
                loss, old_val, q_state = update(
                    q_state,
                    data.observations["image"].numpy(),
                    data.observations["arrow"].numpy(),
                    data.actions.numpy(),
                    data.next_observations["image"].numpy(),
                    data.next_observations["arrow"].numpy(),
                    data.rewards.flatten().numpy(),
                    data.dones.flatten().numpy(),
                )

                if global_step % 100 == 0:
                    writer.add_scalar(
                        "losses/td_loss", jax.device_get(loss), global_step
                    )
                    writer.add_scalar(
                        "losses/q_values", jax.device_get(old_val).mean(), global_step
                    )
                    print("SPS:", int(global_step / (time.time() - start_time)))
                    writer.add_scalar(
                        "charts/SPS",
                        int(global_step / (time.time() - start_time)),
                        global_step,
                    )

            # update target network
            if global_step % args.target_network_frequency == 0:
                q_state = q_state.replace(
                    target_params=optax.incremental_update(
                        q_state.params, q_state.target_params, args.tau
                    )
                )

    if args.save_model:
        model_path = f"runs/{run_name}/{args.exp_name}.cleanrl_model"
        with open(model_path, "wb") as f:
            f.write(flax.serialization.to_bytes(q_state.params))
        print(f"model saved to {model_path}")
        from cleanrl_utils.evals.dqn_jax_eval import evaluate

        episodic_returns = evaluate(
            model_path,
            make_env,
            args.env_id,
            eval_episodes=10,
            run_name=f"{run_name}-eval",
            Model=QNetwork,
            epsilon=0.05,
        )
        for idx, episodic_return in enumerate(episodic_returns):
            writer.add_scalar("eval/episodic_return", episodic_return, idx)

        if args.upload_model:
            from cleanrl_utils.huggingface import push_to_hub

            repo_name = f"{args.env_id}-{args.exp_name}-seed{args.seed}"
            repo_id = f"{args.hf_entity}/{repo_name}" if args.hf_entity else repo_name
            push_to_hub(
                args,
                episodic_returns,
                repo_id,
                "DQN",
                f"runs/{run_name}",
                f"videos/{run_name}-eval",
            )

    envs.close()
    writer.close()
