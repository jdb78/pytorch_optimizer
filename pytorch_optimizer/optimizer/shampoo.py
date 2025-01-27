import torch
from torch.optim.optimizer import Optimizer

from pytorch_optimizer.base.exception import NoSparseGradientError
from pytorch_optimizer.base.optimizer import BaseOptimizer
from pytorch_optimizer.base.types import BETAS, CLOSURE, DEFAULTS, LOSS, PARAMETERS
from pytorch_optimizer.optimizer.shampoo_utils import (
    AdaGradGraft,
    Graft,
    LayerWiseGrafting,
    PreConditioner,
    PreConditionerType,
    RMSPropGraft,
    SGDGraft,
    SQRTNGraft,
    compute_power_svd,
)


class Shampoo(Optimizer, BaseOptimizer):
    r"""Preconditioned Stochastic Tensor Optimization.

    :param params: PARAMETERS. iterable of parameters to optimize or dicts defining parameter groups.
    :param lr: float. learning rate.
    :param momentum: float. momentum.
    :param weight_decay: float. weight decay (L2 penalty).
    :param preconditioning_compute_steps: int. performance tuning params for controlling memory and compute
        requirements. How often to compute pre-conditioner.
    :param matrix_eps: float. term added to the denominator to improve numerical stability.
    """

    def __init__(
        self,
        params: PARAMETERS,
        lr: float = 1e-3,
        momentum: float = 0.0,
        weight_decay: float = 0.0,
        preconditioning_compute_steps: int = 1,
        matrix_eps: float = 1e-6,
    ):
        self.lr = lr
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.preconditioning_compute_steps = preconditioning_compute_steps
        self.matrix_eps = matrix_eps

        self.validate_parameters()

        defaults: DEFAULTS = {'lr': lr, 'momentum': momentum, 'weight_decay': weight_decay}
        super().__init__(params, defaults)

    def validate_parameters(self):
        self.validate_learning_rate(self.lr)
        self.validate_momentum(self.momentum)
        self.validate_weight_decay(self.weight_decay)
        self.validate_update_frequency(self.preconditioning_compute_steps)
        self.validate_epsilon(self.matrix_eps)

    def __str__(self) -> str:
        return 'Shampoo'

    @torch.no_grad()
    def reset(self):
        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]

                state['step'] = 0

    @torch.no_grad()
    def step(self, closure: CLOSURE = None) -> LOSS:
        loss: LOSS = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            momentum, weight_decay = group['momentum'], group['weight_decay']
            for p in group['params']:
                if p.grad is None:
                    continue

                grad = p.grad
                if grad.is_sparse:
                    raise NoSparseGradientError(str(self))

                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0

                    if momentum > 0.0:
                        state['momentum_buffer'] = grad.clone()

                    for dim_id, dim in enumerate(grad.size()):
                        state[f'pre_cond_{dim_id}'] = self.matrix_eps * torch.eye(dim, out=grad.new(dim, dim))
                        state[f'inv_pre_cond_{dim_id}'] = grad.new(dim, dim).zero_()

                if momentum > 0.0:
                    grad.mul_(1.0 - momentum).add_(state['momentum_buffer'], alpha=momentum)

                if weight_decay > 0.0:
                    grad.add_(p, alpha=weight_decay)

                order: int = grad.ndimension()
                original_size: int = grad.size()
                for dim_id, dim in enumerate(grad.size()):
                    pre_cond, inv_pre_cond = state[f'pre_cond_{dim_id}'], state[f'inv_pre_cond_{dim_id}']

                    grad = grad.transpose_(0, dim_id).contiguous()
                    transposed_size = grad.size()

                    grad = grad.view(dim, -1)
                    grad_t = grad.t()

                    pre_cond.add_(grad @ grad_t)
                    if state['step'] % self.preconditioning_compute_steps == 0:
                        inv_pre_cond.copy_(compute_power_svd(pre_cond, -1.0 / order))

                    if dim_id == order - 1:
                        grad = grad_t @ inv_pre_cond
                        grad = grad.view(original_size)
                    else:
                        grad = inv_pre_cond @ grad
                        grad = grad.view(transposed_size)

                state['step'] += 1
                state['momentum_buffer'] = grad

                p.add_(grad, alpha=-group['lr'])

        return loss


class ScalableShampoo(Optimizer, BaseOptimizer):
    r"""Scalable Preconditioned Stochastic Tensor Optimization.

        Reference : https://github.com/google-research/google-research/blob/master/scalable_shampoo/pytorch/shampoo.py.

    :param params: PARAMETERS. iterable of parameters to optimize or dicts defining parameter groups.
    :param lr: float. learning rate.
    :param betas: BETAS. beta1, beta2.
    :param moving_average_for_momentum: bool. perform moving_average for momentum (beta1).
    :param weight_decay: float. weight decay (L2 penalty).
    :param decoupled_weight_decay: bool. use decoupled weight_decay.
    :param decoupled_learning_rate: bool. use decoupled lr, otherwise couple it w/ preconditioned gradient.
    :param inverse_exponent_override: int. fixed exponent for pre-conditioner, if > 0.
    :param start_preconditioning_step: int.
    :param preconditioning_compute_steps: int. performance tuning params for controlling memory and compute
        requirements. How often to compute pre-conditioner. Ideally, 1 is the best. However, the current implementation
        doesn't work on the distributed environment (there are no statistics & pre-conditioners sync among replicas),
        compute on the GPU (not CPU) and the precision is fp32 (not fp64).
        Also, followed by the paper, `preconditioning_compute_steps` does not have a significant effect on the
        performance. So, If you have a problem with the speed, try to set this step bigger (e.g. 1000).
    :param statistics_compute_steps: int. How often to compute statistics. usually set to 1 (or 10).
    :param block_size: int. Block size for large layers (if > 0).
        Block size = 1 ==> Adagrad (Don't do this, extremely inefficient!)
        Block size should be as large as feasible under memory/time constraints.
    :param no_preconditioning_for_layers_with_dim_gt: int. avoid preconditioning large layers to reduce overall memory.
    :param shape_interpretation: bool. Automatic shape interpretation (for eg: [4, 3, 1024, 512] would
        result in 12 x [1024, 512] L and R statistics. Disabled by default which results in Shampoo constructing
        statistics [4, 4], [3, 3], [1024, 1024], [512, 512].
    :param graft_type: int. type of grafting (SGD or AdaGrad or RMSProp or SQRT_N or None).
    :param pre_conditioner_type: int. type of pre-conditioner.
    :param nesterov: bool. Nesterov momentum.
    :param diagonal_eps: float. term added to the denominator to improve numerical stability.
    :param matrix_eps: float. term added to the denominator to improve numerical stability.
    :param use_svd: bool. use SVD instead of Schur-Newton method to calculate M^{-1/p}.
        Theoretically, Schur-Newton method is faster than SVD method. However, the inefficiency of the loop code and
        proper svd kernel, SVD is much faster in some cases (usually in case of small models).
        see https://github.com/kozistr/pytorch_optimizer/pull/103
    """

    def __init__(
        self,
        params: PARAMETERS,
        lr: float = 1e-3,
        betas: BETAS = (0.9, 0.999),
        moving_average_for_momentum: bool = False,
        weight_decay: float = 0.0,
        decoupled_weight_decay: bool = False,
        decoupled_learning_rate: bool = True,
        inverse_exponent_override: int = 0,
        start_preconditioning_step: int = 5,
        preconditioning_compute_steps: int = 1,
        statistics_compute_steps: int = 1,
        block_size: int = 256,
        no_preconditioning_for_layers_with_dim_gt: int = 8192,
        shape_interpretation: bool = True,
        graft_type: int = LayerWiseGrafting.SGD,
        pre_conditioner_type: int = PreConditionerType.ALL,
        nesterov: bool = True,
        diagonal_eps: float = 1e-10,
        matrix_eps: float = 1e-6,
        use_svd: bool = False,
    ):
        self.lr = lr
        self.betas = betas
        self.moving_average_for_momentum = moving_average_for_momentum
        self.weight_decay = weight_decay
        self.decoupled_weight_decay = decoupled_weight_decay
        self.decoupled_learning_rate = decoupled_learning_rate
        self.inverse_exponent_override = inverse_exponent_override
        self.start_preconditioning_step = start_preconditioning_step
        self.preconditioning_compute_steps = preconditioning_compute_steps
        self.statistics_compute_steps = statistics_compute_steps
        self.block_size = block_size
        self.no_preconditioning_for_layers_with_dim_gt = no_preconditioning_for_layers_with_dim_gt
        self.shape_interpretation = shape_interpretation
        self.graft_type = graft_type
        self.pre_conditioner_type = pre_conditioner_type
        self.nesterov = nesterov
        self.diagonal_eps = diagonal_eps
        self.matrix_eps = matrix_eps
        self.use_svd = use_svd

        self.validate_parameters()

        defaults: DEFAULTS = {
            'lr': lr,
            'betas': betas,
            'weight_decay': weight_decay,
        }
        super().__init__(params, defaults)

    def validate_parameters(self):
        self.validate_learning_rate(self.lr)
        self.validate_betas(self.betas)
        self.validate_weight_decay(self.weight_decay)
        self.validate_update_frequency(self.start_preconditioning_step)
        self.validate_update_frequency(self.statistics_compute_steps)
        self.validate_update_frequency(self.preconditioning_compute_steps)
        self.validate_epsilon(self.diagonal_eps)
        self.validate_epsilon(self.matrix_eps)

    def __str__(self) -> str:
        return 'ScalableShampoo'

    @torch.no_grad()
    def reset(self):
        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]

                state['step'] = 0
                state['momentum'] = torch.zeros_like(p)
                state['pre_conditioner'] = PreConditioner(
                    p,
                    group['betas'][1],  # beta2
                    self.inverse_exponent_override,
                    self.block_size,
                    self.no_preconditioning_for_layers_with_dim_gt,
                    self.shape_interpretation,
                    self.matrix_eps,
                    self.pre_conditioner_type,
                    self.use_svd,
                )
                if self.graft_type == LayerWiseGrafting.ADAGRAD:
                    state['graft'] = AdaGradGraft(p, self.diagonal_eps)
                elif self.graft_type == LayerWiseGrafting.RMSPROP:
                    state['graft'] = RMSPropGraft(p, self.diagonal_eps)
                elif self.graft_type == LayerWiseGrafting.SGD:
                    state['graft'] = SGDGraft(p)
                elif self.graft_type == LayerWiseGrafting.SQRTN:
                    state['graft'] = SQRTNGraft(p)
                else:
                    state['graft'] = Graft(p)

    def is_precondition_step(self, step: int) -> bool:
        return step >= self.start_preconditioning_step

    @torch.no_grad()
    def step(self, closure: CLOSURE = None) -> LOSS:
        loss: LOSS = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            beta1, beta2 = group['betas']
            for p in group['params']:
                if p.grad is None:
                    continue

                grad = p.grad
                if grad.is_sparse:
                    raise NoSparseGradientError(str(self))

                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0
                    state['momentum'] = torch.zeros_like(p)
                    state['pre_conditioner'] = PreConditioner(
                        p,
                        beta2,
                        self.inverse_exponent_override,
                        self.block_size,
                        self.no_preconditioning_for_layers_with_dim_gt,
                        self.shape_interpretation,
                        self.matrix_eps,
                        self.pre_conditioner_type,
                        self.use_svd,
                    )
                    if self.graft_type == LayerWiseGrafting.ADAGRAD:
                        state['graft'] = AdaGradGraft(p, self.diagonal_eps)
                    elif self.graft_type == LayerWiseGrafting.RMSPROP:
                        state['graft'] = RMSPropGraft(p, self.diagonal_eps)
                    elif self.graft_type == LayerWiseGrafting.SGD:
                        state['graft'] = SGDGraft(p)
                    elif self.graft_type == LayerWiseGrafting.SQRTN:
                        state['graft'] = SQRTNGraft(p)
                    else:
                        state['graft'] = Graft(p)

                state['step'] += 1
                pre_conditioner, graft = state['pre_conditioner'], state['graft']

                is_precondition_step: bool = self.is_precondition_step(state['step'])

                graft.add_statistics(grad, beta2)
                if state['step'] % self.statistics_compute_steps == 0:
                    pre_conditioner.add_statistics(grad)
                if state['step'] % self.preconditioning_compute_steps == 0:
                    pre_conditioner.compute_pre_conditioners()

                pre_conditioner_multiplier: float = group['lr'] if not self.decoupled_learning_rate else 1.0
                graft_grad: torch.Tensor = graft.precondition_gradient(grad * pre_conditioner_multiplier)
                shampoo_grad: torch.Tensor = grad
                if is_precondition_step:
                    shampoo_grad = pre_conditioner.preconditioned_grad(grad)

                if self.graft_type != LayerWiseGrafting.NONE:
                    graft_norm = torch.norm(graft_grad)
                    shampoo_norm = torch.norm(shampoo_grad)

                    shampoo_grad.mul_(graft_norm / (shampoo_norm + 1e-16))

                if group['weight_decay'] > 0.0:
                    if not self.decoupled_weight_decay:
                        shampoo_grad.add_(p, alpha=group['weight_decay'])
                        graft_grad.add_(p, alpha=group['weight_decay'])
                    else:
                        shampoo_grad.mul_(1.0 - group['lr'] * group['weight_decay'])
                        graft_grad.mul_(1.0 - group['lr'] * group['weight_decay'])

                state['momentum'].mul_(beta1).add_(shampoo_grad)
                graft_momentum = graft.update_momentum(grad, beta1)

                if is_precondition_step:
                    momentum_update = state['momentum']
                    wd_update = shampoo_grad
                else:
                    momentum_update = graft_momentum
                    wd_update = graft_grad

                if self.nesterov:
                    w: float = (1.0 - beta1) if self.moving_average_for_momentum else 1.0
                    wd_update.mul_(w)

                    momentum_update.mul_(beta1).add_(wd_update)

                p.add_(momentum_update, alpha=-group['lr'])

        return loss
