from typing import Optional

from storch.tensor import Tensor, StochasticTensor, CostTensor, IndependentTensor
import torch
from storch.util import print_graph
import storch
from storch.typing import AnyTensor


_cost_tensors: [CostTensor] = []


def denote_independent(
    tensor: AnyTensor, dim: int, plate_name: str
) -> IndependentTensor:
    """
    Denote the given dimensions on the tensor as being independent, that is, batched dimensions.
    It will automatically put these dimensions to the right.
    :param tensor:
    :param dims:
    :param plate_name: Name of the plate. Reused if called again
    :return:
    """
    if (
        storch.wrappers._context_stochastic
        or storch.wrappers._context_deterministic > 0
    ):
        raise RuntimeError(
            "Cannot create independent tensors within a deterministic or stochastic context."
        )
    if isinstance(tensor, torch.Tensor):
        if dim != 0:
            tensor = tensor.transpose(dim, 0)
        return IndependentTensor(tensor, [], [], plate_name)
    else:
        t_tensor = tensor._tensor
        if dim != 0:
            t_tensor = t_tensor.transpose(dim, 0)
        name = tensor.name + "_indep_" + plate_name if tensor.name else plate_name
        return IndependentTensor(t_tensor, tensor.plates, [tensor], name)


def add_cost(cost: Tensor, name: str):
    if cost.event_shape != ():
        raise ValueError("Can only register cost functions with empty event shapes")
    if not name:
        raise ValueError(
            "No name provided to register cost node. Make sure to register an unique name with the cost."
        )
    cost = CostTensor(cost._tensor, [cost], cost.plates, name)
    if torch.is_grad_enabled():
        storch.inference._cost_tensors.append(cost)
    return cost


def backward(
    retain_graph=False, debug=False, print_costs=False
) -> (torch.Tensor, torch.Tensor):
    """

    :param retain_graph: If set to False, it will deregister the added cost nodes. Should usually be set to False.
    :param debug: Prints debug information on the backwards call.
    :param accum_grads: Saves gradient information in stochastic nodes. Note that this is an expensive option as it
    requires doing O(n) backward calls for each stochastic node sampled multiple times. Especially if this is a
    hierarchy of multiple samples.
    :return:
    """
    costs: [storch.Tensor] = storch.inference._cost_tensors
    if debug:
        print_graph(costs)

    # Sum of averages of cost node tensors
    total_cost = 0.0
    # Sum of losses that can be backpropagated through in keepgrads without difficult iterations
    accum_loss = 0.0

    stochastic_nodes = set()
    # Loop over different cost nodes
    for c in costs:
        avg_cost = c
        for plate in c.multi_dim_plates():
            # Do not detach the weights when reducing. This is used in for example expectations to weight the
            # different costs.
            avg_cost = plate.reduce(avg_cost, detach_weights=False)
        if print_costs:
            print(c.name, ":", avg_cost._tensor.item())
        total_cost += avg_cost
        for parent in c.walk_parents(depth_first=False):
            # Instance check here instead of parent.stochastic, as backward methods are only used on these.
            if isinstance(parent, StochasticTensor):
                stochastic_nodes.add(parent)
            if (
                not isinstance(parent, StochasticTensor)
                or not parent.requires_grad
                or not parent.sampling_method.adds_loss(parent, c)
            ):
                continue

            # Transpose the parent stochastic tensor, so that its shape is the same as the cost but the event shape, and
            # possibly extra dimensions...?
            parent_tensor = parent._tensor
            reduced_cost = c
            parent_plates = list(parent.multi_dim_plates())
            # Reduce all plates that are in the cost node but not in the parent node
            for plate in c.multi_dim_plates():
                if plate not in parent_plates:
                    reduced_cost = plate.reduce(c, detach_weights=True)

            # Align the parent tensor so that the plate dimensions are in the same order as the cost tensor
            for index_c, plate in enumerate(reduced_cost.multi_dim_plates()):
                index_p = parent_plates.index(plate)
                if index_c != index_p:
                    parent_tensor = parent_tensor.transpose(index_p, index_c)
                    parent_plates[index_p], parent_plates[index_c] = (
                        parent_plates[index_c],
                        parent_plates[index_p],
                    )
            # Add empty (k=1) plates to new parent
            for plate in parent.plates:
                if plate not in parent_plates:
                    parent_plates.append(plate)

            # Create new storch Tensors with different order of plates for the cost and parent
            new_parent = storch.tensor._StochasticTensorBase(
                parent_tensor,
                [],
                parent_plates,
                parent.name,
                parent.sampling_method,
                parent.distribution,
                parent._requires_grad,
                parent.n,
            )
            print("_---------------------------------_")
            print("new_parent", new_parent)
            print("new_cost", reduced_cost)
            # Fake the new parent to be the old parent within the graph by mimicing its place in the graph
            new_parent._parents = parent._parents
            for p, has_link in new_parent._parents:
                p._children.append((new_parent, has_link))
            new_parent._children = parent._children
            print(parent.sampling_method)
            cost_per_sample = parent.sampling_method._estimator(
                new_parent, reduced_cost
            )
            print("estimator", cost_per_sample)
            # The backwards call for reparameterization happens in the
            # backwards call for the costs themselves.
            # Now mean_cost has the same shape as parent.batch_shape
            final_reduced_cost = cost_per_sample
            for plate in cost_per_sample.multi_dim_plates():
                final_reduced_cost = plate.reduce(
                    final_reduced_cost, detach_weights=True
                )
            if final_reduced_cost.ndim == 1:
                final_reduced_cost = final_reduced_cost.squeeze(0)
            print("final cost", final_reduced_cost)
            accum_loss += final_reduced_cost

        # Compute gradients for the cost nodes themselves, if they require one.
        if avg_cost.requires_grad:
            accum_loss += avg_cost

    if isinstance(accum_loss, storch.Tensor) and accum_loss._tensor.requires_grad:
        accum_loss._tensor.backward(retain_graph=retain_graph)

    for s_node in stochastic_nodes:
        s_node.sampling_method._update_parameters()

    if not retain_graph:
        reset()

    return total_cost._tensor, accum_loss._tensor


def reset():
    storch.inference._cost_tensors = []
