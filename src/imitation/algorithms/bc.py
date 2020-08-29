"""Behavioural Cloning (BC).

Trains policy by applying supervised learning to a fixed dataset of (observation,
action) pairs generated by some expert demonstrator.
"""

from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple, Type, Union

import gym
import torch as th
from stable_baselines3.common import logger, policies, utils
from tqdm.autonotebook import trange

from imitation.policies import base


def reconstruct_policy(
    policy_path: str,
    device: Union[th.device, str] = "auto",
) -> policies.BasePolicy:
    """Reconstruct a saved policy.

    Args:
        policy_path: path where `.save_policy()` has been run.
        device: device on which to load the policy.

    Returns:
        policy: policy with reloaded weights.
    """
    policy = th.load(policy_path, map_location=utils.get_device(device))
    assert isinstance(policy, policies.BasePolicy)
    return policy


class ConstantLRSchedule:
    """A callable that returns a constant learning rate."""

    def __init__(self, lr: float = 1e-3):
        """
        Args:
            lr: the constant learning rate that calls to this object will return.
        """
        self.lr = lr

    def __call__(self, _):
        """
        Returns the constant learning rate.
        """
        return self.lr


BCDataLoaderDucktype = Iterable[Mapping[str, th.Tensor]]


class BC:
    # TODO(scottemmons): pass BasePolicy into BC directly (rather than passing its
    #  arguments)
    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        *,
        policy_class: Type[policies.BasePolicy] = base.FeedForward32Policy,
        policy_kwargs: Optional[Mapping[str, Any]] = None,
        expert_dataloader: Optional[BCDataLoaderDucktype] = None,
        optimizer_cls: Type[th.optim.Optimizer] = th.optim.Adam,
        optimizer_kwargs: Optional[Dict[str, Any]] = None,
        ent_weight: float = 1e-3,
        l2_weight: float = 0.0,
        device: Union[str, th.device] = "auto",
    ):
        """Behavioral cloning (BC).

        Recovers a policy via supervised learning on observation-action Tensor
        pairs, sampled from a Torch DataLoader or any Iterator that ducktypes
        `torch.utils.data.DataLoader`.

        Args:
            observation_space: the observation space of the environment.
            action_space: the action space of the environment.
            policy_class: used to instantiate imitation policy.
            policy_kwargs: keyword arguments passed to policy's constructor.
            expert_dataloader: If not None, then immediately call
                  `self.set_expert_dataloader(expert_data)` during initialization.
            optimizer_cls: optimiser to use for supervised training.
            optimizer_kwargs: keyword arguments, excluding learning rate and
                  weight decay, for optimiser construction.
            ent_weight: scaling applied to the policy's entropy regularization.
            l2_weight: scaling applied to the policy's L2 regularization.
            device: name/identity of device to place policy on.
        """
        if optimizer_kwargs:
            if "weight_decay" in optimizer_kwargs:
                raise ValueError("Use the parameter l2_weight instead of weight_decay.")

        self.action_space = action_space
        self.observation_space = observation_space
        self.policy_class = policy_class
        self.device = device = utils.get_device(device)
        self.policy_kwargs = dict(
            observation_space=self.observation_space,
            action_space=self.action_space,
            lr_schedule=ConstantLRSchedule(),
            device=self.device,
        )
        self.policy_kwargs.update(policy_kwargs or {})
        self.device = utils.get_device(device)

        self.policy = self.policy_class(**self.policy_kwargs).to(
            self.device
        )  # pytype: disable=not-instantiable
        optimizer_kwargs = optimizer_kwargs or {}
        self.optimizer = optimizer_cls(self.policy.parameters(), **optimizer_kwargs)

        self.expert_dataloader: Optional[BCDataLoaderDucktype] = None
        self.ent_weight = ent_weight
        self.l2_weight = l2_weight

        if expert_dataloader is not None:
            self.set_expert_dataloader(expert_dataloader)

    def set_expert_dataloader(self, expert_dataloader: BCDataLoaderDucktype) -> None:
        """Set the expert dataloader, which yields batches of obs-act pairs.

        Changing the expert dataloader on-demand is useful for DAgger and other
        interactive algorithms.

        Args:
             expert_dataloader: Either a Torch `DataLoader` that yields dictionaries
                containing "obs" and "acts" tensors, or any other iterator that
                yields the same.
        """
        self.expert_dataloader = expert_dataloader

    def _calculate_loss(self, obs, acts) -> Tuple[th.Tensor, Dict[str, float]]:
        """
        Calculate the supervised learning loss used to train the behavioral clone.

        Args:
            obs: The observations seen by the expert.
            acts: The actions taken by the expert.

        Returns:
            loss: The supervised learning loss for the behavioral clone to optimize.
            stats_dict: Statistics about the learning process to be logged.

        """
        _, log_prob, entropy = self.policy.evaluate_actions(obs, acts)
        prob_true_act = th.exp(log_prob).mean()
        log_prob = log_prob.mean()
        entropy = entropy.mean()

        l2_norms = [th.sum(th.square(w)) for w in self.policy.parameters()]
        l2_norm = sum(l2_norms) / 2  # divide by 2 to cancel with gradient of square

        ent_loss = -self.ent_weight * entropy
        neglogp = -log_prob
        l2_loss = self.l2_weight * l2_norm
        loss = neglogp + ent_loss + l2_loss

        stats_dict = dict(
            neglogp=neglogp.item(),
            loss=loss.item(),
            entropy=entropy.item(),
            ent_loss=ent_loss.item(),
            prob_true_act=prob_true_act.item(),
            l2_norm=l2_norm.item(),
            l2_loss=l2_loss.item(),
        )

        return loss, stats_dict

    def train(
        self,
        n_epochs: int = 100,
        *,
        on_epoch_end: Callable[[dict], None] = None,
        log_interval: int = 100,
    ):
        """Train with supervised learning for some number of epochs.

        Here an 'epoch' is just a complete pass through the expert data loader,
        as set by `self.set_expert_dataloader()`.

        Args:
            n_epochs: Number of complete passes made through dataset.
            on_epoch_end: Optional callback to run at
                the end of each epoch. Will receive all locals from this function as
                dictionary argument (!!).
            log_interval: Log stats after every log_interval batches.
        """
        samples_so_far = 0
        batch_num = 0
        for epoch_num in trange(n_epochs, desc="BC epoch"):
            for batch in self.expert_dataloader:
                batch_num += 1
                batch_size = len(batch["obs"])
                assert batch_size > 0
                samples_so_far += batch_size

                obs = batch["obs"].to(self.device)
                acts = batch["acts"].to(self.device)
                loss, stats_dict = self._calculate_loss(obs, acts)

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                stats_dict["epoch_num"] = epoch_num
                stats_dict["n_updates"] = batch_num
                stats_dict["batch_size"] = batch_size

                if batch_num % log_interval == 0:
                    for k, v in stats_dict.items():
                        logger.record(k, v)
                    logger.dump(batch_num)

            if on_epoch_end is not None:
                on_epoch_end(locals())

    def save_policy(self, policy_path: str) -> None:
        """Save policy to a path. Can be reloaded by `.reconstruct_policy()`.

        Args:
            policy_path: path to save policy to.
        """
        th.save(self.policy, policy_path)
