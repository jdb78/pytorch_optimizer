from typing import List

import numpy as np
import pytest
import torch
from torch import nn

from pytorch_optimizer.optimizer.utils import (
    clip_grad_norm,
    compute_power,
    disable_running_stats,
    enable_running_stats,
    get_optimizer_parameters,
    has_overflow,
    is_valid_parameters,
    merge_small_dims,
    neuron_mean,
    neuron_norm,
    normalize_gradient,
    unit_norm,
)
from tests.utils import Example


def test_has_overflow():
    assert has_overflow(np.inf)
    assert has_overflow(np.nan)
    assert not has_overflow(torch.Tensor([1]))


def test_normalized_gradient():
    x = torch.arange(0, 10, dtype=torch.float32)

    np.testing.assert_allclose(
        normalize_gradient(x).numpy(),
        np.asarray([0.0000, 0.3303, 0.6606, 0.9909, 1.3212, 1.6514, 1.9817, 2.3120, 2.6423, 2.9726]),
        rtol=1e-4,
        atol=1e-4,
    )

    np.testing.assert_allclose(
        normalize_gradient(x.view(1, 10), use_channels=True).numpy(),
        np.asarray([[0.0000, 0.3303, 0.6606, 0.9909, 1.3212, 1.6514, 1.9817, 2.3120, 2.6423, 2.9726]]),
        rtol=1e-4,
        atol=1e-4,
    )


def test_clip_grad_norm():
    x = torch.arange(0, 10, dtype=torch.float32, requires_grad=True)
    x.grad = torch.arange(0, 10, dtype=torch.float32)

    np.testing.assert_approx_equal(clip_grad_norm(x), 16.881943016134134, significant=4)
    np.testing.assert_approx_equal(clip_grad_norm(x, max_norm=2), 16.881943016134134, significant=4)


def test_unit_norm():
    x = torch.arange(0, 10, dtype=torch.float32)

    np.testing.assert_approx_equal(unit_norm(x).numpy(), 16.8819, significant=4)
    np.testing.assert_approx_equal(unit_norm(x.view(1, 10)).numpy(), 16.8819, significant=4)
    np.testing.assert_approx_equal(unit_norm(x.view(1, 10, 1, 1)).numpy(), 16.8819, significant=4)
    np.testing.assert_approx_equal(unit_norm(x.view(1, 10, 1, 1, 1, 1)).numpy(), 16.8819, significant=4)


def test_neuron_mean_norm():
    x = torch.arange(-5, 5, dtype=torch.float32)

    with pytest.raises(ValueError):
        neuron_mean(x)

    np.testing.assert_array_equal(
        neuron_mean(x.view(-1, 1)).numpy(),
        np.asarray([[-5.0], [-4.0], [-3.0], [-2.0], [-1.0], [0.0], [1.0], [2.0], [3.0], [4.0]]),
    )
    np.testing.assert_array_equal(
        neuron_norm(x).numpy(), np.asarray([5.0, 4.0, 3.0, 2.0, 1.0, 0.0, 1.0, 2.0, 3.0, 4.0])
    )
    np.testing.assert_array_equal(
        neuron_norm(x.view(-1, 1)).numpy(),
        np.asarray([[5.0], [4.0], [3.0], [2.0], [1.0], [0.0], [1.0], [2.0], [3.0], [4.0]]),
    )


def test_get_optimizer_parameters():
    model: nn.Module = Example()
    wd_ban_list: List[str] = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']

    before_parameters = list(model.named_parameters())
    after_parameters = get_optimizer_parameters(model, weight_decay=1e-3, wd_ban_list=wd_ban_list)

    for before, after in zip(before_parameters, after_parameters):
        layer_name: str = before[0]
        if layer_name.find('bias') != -1 or layer_name in wd_ban_list:
            assert after['weight_decay'] == 0.0


def test_is_valid_parameters():
    model: nn.Module = Example()
    wd_ban_list: List[str] = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']

    after_parameters = get_optimizer_parameters(model, weight_decay=1e-3, wd_ban_list=wd_ban_list)

    assert is_valid_parameters(after_parameters)


def test_running_stats():
    model = nn.Sequential(
        nn.Linear(1, 1),
        nn.BatchNorm2d(1),
    )
    model[1].momentum = 0.1

    disable_running_stats(model)

    assert (model[1].momentum == 0) and (model[1].backup_momentum == 0.1)

    enable_running_stats(model)

    assert model[1].momentum == 0.1


def test_compute_power():
    # case 1 : len(x.shape) == 1
    x = compute_power(torch.zeros((1,)), p=1)
    assert torch.tensor([1000000.0]) == x

    # case 2 : len(x.shape) != 1 and x.shape[0] == 1
    x = compute_power(torch.zeros((1, 2)), p=1)
    assert torch.tensor([1.0]) == x

    # case 3 : len(x.shape) != 1 and x.shape[0] != 1, n&n-1 != 0
    x = compute_power(torch.ones((2, 2)), p=5)
    np.testing.assert_array_almost_equal(
        np.asarray([[7.3464, -6.4758], [-6.4758, 7.3464]]),
        x.numpy(),
        decimal=3,
    )

    # case 4 : len(x.shape) != 1 and x.shape[0] != 1, n&n-1 == 0
    x = compute_power(torch.ones((2, 2)), p=32)
    np.testing.assert_array_almost_equal(
        np.asarray([[1.1527, -0.3520], [-0.3520, 1.1527]]),
        x.numpy(),
        decimal=3,
    )


def test_merge_small_dims():
    case1 = [1, 2, 512, 1, 2048, 1, 3, 4]
    expected_case1 = [1024, 2048, 12]
    assert expected_case1 == merge_small_dims(case1, max_dim=1024)

    case2 = [1, 2, 768, 1, 2048]
    expected_case2 = [2, 768, 2048]
    assert expected_case2 == merge_small_dims(case2, max_dim=1024)
