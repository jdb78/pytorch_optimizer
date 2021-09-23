from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import torch

CLOSURE = Optional[Callable[[], float]]
LOSS = Optional[float]
BETAS = Tuple[float, float]
DEFAULTS = Dict[str, Any]
PARAMETERS = Union[Iterable[Dict[str, Any]], Iterable[torch.Tensor]]
PARAM_GROUP = Dict
PARAM_GROUPS = List[PARAM_GROUP]
STATE = Dict[str, Any]
BUFFER = List[List[Optional[torch.Tensor]]]
