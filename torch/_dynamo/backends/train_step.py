from contextlib import contextmanager
from copy import copy
from typing import Any, Dict, List

import torch
import torch.utils._pytree as pytree
from torch import fx
from torch._dynamo import register_backend
from torch._dynamo.backends.registry import lookup_backend
from torch._subclasses.fake_tensor import FakeTensor, FakeTensorMode

from torch.func import functionalize
from torch.fx.experimental.proxy_tensor import make_fx
from torch.fx.interpreter import Interpreter
from torch.nn.utils import stateless


@contextmanager
def _rematerialize_optimizer(
    opt: torch.optim.Optimizer,
    named_states: Dict[str, Any],
    params: Dict[str, torch.nn.Parameter],
):
    assert opt is not None

    # update opt.state with proxy tensors
    orig_states: Dict[str, Any] = copy(opt.state)
    if named_states:
        for n in named_states:
            # opt.state's key type is string, but optimizer uses Parameter as keys
            opt.state[params[n]] = named_states[n]  # type: ignore[index]

    # FIXME: support multiple parameter groups
    param_group = opt.param_groups[0]
    orig_params = param_group["params"]
    # FIXME(@mrshenli): exclude buffers
    param_group["params"] = params.values()

    try:
        yield
    finally:
        param_group["params"] = orig_params
        opt.state.update(orig_states)


def train_step_compiler(backend_compile_fn):
    """Note [Train Step Compile]

    Usually, torch.compile() allows graph-breaks and compiles pairs of forward (+backward) by
    extracting sections of forward from python programs and using AotAutograd to produce corresponding
    chunks of backwards, tying it back together with an AotFunction.

    Instead, TrainStepCompiler assumes the user compiles a full train_step function complete with calls to
    .backward(), optimizer step(), and zero_grad().  It additionally requires no graph-breaks.

    Args:
        backend_compile_fn (callable): A dynamo compiler function, to be invoked to compile each subgraph.
    """

    def _compile_fn(mod: fx.GraphModule, fake_inputs: List[torch.Tensor]):
        print(mod.graph)
        torch.set_grad_enabled(True)
        torch._dynamo.utils.assert_no_fake_params_or_buffers(mod)
        assert len(fake_inputs) > 0, "Expected at least one input"
        fake_mode = fake_inputs[0].fake_mode
        assert isinstance(
            fake_mode, FakeTensorMode
        ), "Expected a valid FakeTensorMode on dynamo inputs"

        def fakeify_inputs(flat_args):
            def convert(idx, x):
                # todo: do we expect symint inputs?
                assert isinstance(x, torch.Tensor)
                return fake_mode.from_tensor(x, static_shapes=False)

            return [convert(idx, x) for idx, x in enumerate(flat_args)]

        # OK whats going on dynamo? when i simplify to single-layer model, i get 2 variants of each name?
        # (Pdb) p params.keys()
        # dict_keys(['model_lay.weight', 'model_lay.bias', 'model.lay.weight', 'model.lay.bias'])
        params = {
            **dict(mod.named_parameters(remove_duplicate=False)),
            **dict(mod.named_buffers(remove_duplicate=False)),
        }
        params_flat, params_spec = pytree.tree_flatten(params)
        params_len = len(params_flat)
        fake_params_flat = fakeify_inputs(params_flat)

        opt = mod.__optimizer_0


        def functional_call(*lifted_args, **kwargs):
            """Call the dynamo graphmodule in a functional way safe for tracing
            (lifts module parameters and optimizer states as inputs)
            """

            _params = lifted_args[:params_len]
            _params_dict = pytree.tree_unflatten(_params, params_spec)
            _user_args = lifted_args[params_len + named_states_len :]
            with stateless._reparametrize_module(
                mod, _params_dict
            ), _rematerialize_optimizer(opt, None, _params_dict):
                out = mod(*_user_args, **kwargs)

            if not isinstance(out, (tuple, list)):
                raise RuntimeError(
                    "Graph output must be a tuple() to avoid pytree processing of the ouputs."
                )
            return out

        """
        Step 1: Warm up the optimizer
        - this adds state tensors to the previously empty optimizer state dict

        """
        named_states_len = 0
        # _ = functional_call(*fake_params_flat + fake_inputs)
        dev = params_flat[0].device
        # so we don't mutate the real params when running the warmup...
        # copied_params = [p.clone().detach() for p in params_flat]
        # running with fake inputs and fixing-up the opt states is hard, since the opt-states
        # get keyed off _mutated_ faketensor module params, which have diff ids than orig fake module params
        # real_inputs = [
        #     torch.randn(i.shape, dtype=i.dtype, device=dev) for i in fake_inputs
        # ]
        # print(f"compiled param0 {params_flat[0]}")
        for fake_param in fake_params_flat:
            print(f"{id(fake_param)}")
        fake_mode.allow_non_fake_inputs = True
        first_loss = functional_call(*fake_params_flat + fake_inputs)
        fake_mode.allow_non_fake_inputs = False
        # print(f"compiled param0 {params_flat[0]}, first_loss:  {first_loss}")
        # Convert the fake optimizer states to real
        for fake_param, state_dict in opt.state.items():
            print(f"fake: {id(fake_param)}")
            for name, state in state_dict.items():
                # # some of the states are singleton cpu tensors...
                if isinstance(state, FakeTensor):
                    # can we assume always init with zeros?
                    state_dict[name] = torch.zeros(state.shape, dtype=state.dtype, device=dev)
                state_dict[name].zero_()
        # Build a mapping to use for reparametrizing the optimizer during tracing
        named_states = {}
        for n, p in pytree.tree_unflatten(fake_params_flat, params_spec).items():
        # for n, p in params.items():
            if p in opt.state:
                named_states[n] = opt.state[p]  # type: ignore[index]

        named_states_flat, named_states_spec = pytree.tree_flatten(named_states)
        fake_named_states_flat = fakeify_inputs(named_states_flat)
        named_states_len = len(named_states_flat)
        full_fake_args = fake_params_flat + fake_named_states_flat + fake_inputs

        """
        Step 2: Trace the full graph
        """

        def functional_call_2(*lifted_args, **kwargs):
            """Call the dynamo graphmodule in a functional way safe for tracing
            (lifts module parameters and optimizer states as inputs)
            """

            _params = lifted_args[:params_len]
            _params_dict = pytree.tree_unflatten(_params, params_spec)
            _named_states = lifted_args[params_len : params_len + named_states_len]
            _named_states_dict = pytree.tree_unflatten(_named_states, named_states_spec)
            _user_args = lifted_args[params_len + named_states_len :]
            with stateless._reparametrize_module(
                mod, _params_dict
            ), _rematerialize_optimizer(opt, _named_states_dict, _params_dict):
                out = mod(*_user_args, **kwargs)

            if not isinstance(out, (tuple, list)):
                raise RuntimeError(
                    "Graph output must be a tuple() to avoid pytree processing of the ouputs."
                )
            return out

        fx_g = make_fx(functional_call_2)(*full_fake_args)
        torch.set_grad_enabled(False)
        print("fx_g")
        print(fx_g)
        """
        Step 3: Functionalize the resulting flattend graph, producing code with copy_ ops
                as an epilogue for any inplace/mutating ops such as optimizer update.
        """

        def retraced_f(*args):
            return Interpreter(fx_g).run(*args)

        with torch.inference_mode():
            functional_fx_g = make_fx(functionalize(retraced_f))(*full_fake_args)

        """
        Step 4: Reverse the calling-convention change we made above with _reparametrize_module,
                and return a function that accepts the arguments as originally provided by dynamo
        """
        print("functional_fx_g.graph")
        print(functional_fx_g.graph)

        def call_without_params(*runtime_args):
            with torch.no_grad():
                return functional_fx_g(
                    *params_flat + named_states_flat + list(runtime_args)
                )

        return call_without_params

    return _compile_fn


train_step_eager = train_step_compiler(lookup_backend("eager"))
register_backend(name="train_step_eager", compiler_fn=train_step_eager)
