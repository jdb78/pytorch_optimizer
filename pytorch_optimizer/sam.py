from typing import Dict

import torch
from torch.optim.optimizer import Optimizer

from pytorch_optimizer.types import CLOSURE, DEFAULTS, PARAM_GROUPS, PARAMETERS


class SAM(Optimizer):
    """
    Reference : https://github.com/davda54/sam
    Example :
        from pytorch_optimizer import SAM
        ...
        model = YourModel()
        base_optimizer =  Ranger21
        optimizer = SAM(model.parameters(), base_optimizer)
        ...
        for input, output in data:
            # first forward-backward pass

            # use this loss for any training statistics
            loss = loss_function(output, model(input))
            loss.backward()
            optimizer.first_step(zero_grad=True)

            # second forward-backward pass
            # make sure to do a full forward pass
            loss_function(output, model(input)).backward()
            optimizer.second_step(zero_grad=True)

    Alternative Example with a single closure-based step function:
        from pytorch_optimizer import SAM
        ...
        model = YourModel()
        base_optimizer =  Ranger21
        optimizer = SAM(model.parameters(), base_optimizer)

        def closure():
            loss = loss_function(output, model(input))
            loss.backward()
            return loss
        ...

        for input, output in data:
            loss = loss_function(output, model(input))
            loss.backward()
            optimizer.step(closure)
            optimizer.zero_grad()
    """

    def __init__(
        self,
        params: PARAMETERS,
        base_optimizer,
        rho: float = 0.05,
        adaptive: bool = False,
        **kwargs,
    ):
        """
        :param params: PARAMETERS. iterable of parameters to optimize or dicts defining parameter groups
        :param base_optimizer: Optimizer.
        :param rho: float. size of the neighborhood for computing the max loss
        :param adaptive: bool. element-wise Adaptive SAM
        :param kwargs: Dict. parameters for optimizer.
        """
        self.rho = rho

        self.check_valid_parameters()

        defaults: DEFAULTS = dict(rho=rho, adaptive=adaptive, **kwargs)
        super().__init__(params, defaults)

        self.base_optimizer = base_optimizer(self.param_groups, **kwargs)
        self.param_groups: PARAM_GROUPS = self.base_optimizer.param_groups

    def check_valid_parameters(self):
        if self.rho < 0.0:
            raise ValueError(f'Invalid rho : {self.rho}')

    @torch.no_grad()
    def first_step(self, zero_grad: bool = False):
        grad_norm = self.grad_norm()
        for group in self.param_groups:
            scale = group['rho'] / (grad_norm + 1e-12)

            for p in group['params']:
                if p.grad is None:
                    continue
                self.state[p]['old_p'] = p.data.clone()
                e_w = (torch.pow(p, 2) if group['adaptive'] else 1.0) * p.grad * scale.to(p)
                # climb to the local maximum "w + e(w)"
                p.add_(e_w)

        if zero_grad:
            self.zero_grad()

    @torch.no_grad()
    def second_step(self, zero_grad: bool = False):
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                # get back to "w" from "w + e(w)"
                p.data = self.state[p]['old_p']

        # do the actual "sharpness-aware" update
        self.base_optimizer.step()

        if zero_grad:
            self.zero_grad()

    @torch.no_grad()
    def step(self, closure: CLOSURE = None):
        if closure is None:
            raise RuntimeError('Sharpness Aware Minimization requires closure')

        # the closure should do a full forward-backward pass
        closure = torch.enable_grad()(closure)

        self.first_step(zero_grad=True)
        closure()
        self.second_step()

    def grad_norm(self) -> torch.Tensor:
        # put everything on the same device, in case of model parallelism
        shared_device = self.param_groups[0]['params'][0].device
        norm = torch.norm(
            torch.stack(
                [
                    ((torch.abs(p) if group['adaptive'] else 1.0) * p.grad).norm(p=2).to(shared_device)
                    for group in self.param_groups
                    for p in group['params']
                    if p.grad is not None
                ]
            ),
            p=2,
        )
        return norm

    def load_state_dict(self, state_dict: Dict):
        super().load_state_dict(state_dict)
        self.base_optimizer.param_groups = self.param_groups
