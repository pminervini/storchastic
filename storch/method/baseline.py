from abc import ABC, abstractmethod
import torch
from storch.tensor import StochasticTensor, CostTensor


class Baseline(ABC, torch.nn.Module):
    def __init__(self):
        super().__init__()

    @abstractmethod
    def compute_baseline(
        self, tensor: StochasticTensor, cost_node: CostTensor
    ) -> torch.Tensor:
        pass


class MovingAverageBaseline(Baseline):
    def __init__(self, exponential_decay=0.95):
        super().__init__()
        self.register_buffer("exponential_decay", torch.tensor(exponential_decay))
        self.register_buffer("moving_average", torch.tensor(0.0))

    def compute_baseline(
        self, tensor: StochasticTensor, cost_node: CostTensor
    ) -> torch.Tensor:
        avg_cost = cost_node.detach_tensor().mean().detach()
        self.moving_average = (
            self.exponential_decay * self.moving_average
            + (1 - self.exponential_decay) * avg_cost
        )
        return self.moving_average


class BatchAverageBaseline(Baseline):
    # Uses the means of the other samples
    def compute_baseline(
        self, tensor: StochasticTensor, costs: CostTensor
    ) -> torch.Tensor:
        if tensor.n == 1:
            raise ValueError(
                "Can only use the batch average baseline if multiple samples are used."
            )
        # TODO: Check dimensions/indexing of 0
        costs = costs.detach_tensor().detach()
        return (costs.sum(0) - costs) / (costs.shape[0] - 1)
