from __future__ import annotations

from typing import Dict, List, Tuple, Union

import accelerate
import baukit
import torch
from transformers import (AutoModelForCausalLM, AutoTokenizer, BatchEncoding,
                          PreTrainedModel, PreTrainedTokenizer)
from transformers.generation.utils import GenerateOutput
from typing_extensions import override

from .intervention.Intervention import (Adhoc, Copy, Get, Intervention, Tensor,
                                        output_intervene)
from .Module import Module
from .Promise import Promise


class Model:
    '''
    A Model represents a wrapper for an LLM

    Attributes
    ----------
        model_name_or_path : str
            name of registered model or path to checkpoint
        graph : PreTrainedModel
            model with weights not initialized 
        tokenizer : PreTrainedTokenizer
        local_model : PreTrainedModel
        output : Union[GenerateOutput, torch.LongTensor]
    '''

    class Invoker:
        '''
        An Invoker represents a context window for running a single prompt which tracks
        all requested interventions applied during the invokation of the prompt

        Class Attributes
        ----------
            execution_graphs : List[List[str]]
                a list of all invocation execution_graph
            prompts : List[str]
                list of all invocation prompts
            promises : Dict[str, Promise]
                dict of all promises for all invocations

        Attributes
        ----------
            model : PreTrainedModel
            prompt : str
            args : List
            kwargs : Dict
        '''

        execution_graphs: List[List[str]] = list()
        prompts: List[str] = list()
        promises: Dict[str, Promise] = dict()

        @classmethod
        def clear(cls) -> None:
            '''
            Clears everything. To be called after model execution and promise fulfillment. 
            '''
            Model.Invoker.execution_graphs.clear()
            Model.Invoker.promises.clear()
            Model.Invoker.prompts.clear()

        @classmethod
        def compile(cls) -> Tuple[List[str], Dict[str, Promise], List[str]]:
            '''
            Returns everything needed to convert all invocations into interventions.

            Returns
            ----------
                List[str]
                    execution graphs
                Dict[str, Promise]
                    promises
                List[str]
                    prompts
            '''
            return Model.Invoker.execution_graphs, Model.Invoker.promises, Model.Invoker.prompts

        def __init__(self, model: Model, prompt: str, *args, **kwargs) -> None:

            self.model = model
            self.prompt = prompt
            self.args = args
            self.kwargs = kwargs

        @property
        def tokens(self) -> List[str]:
            '''
            Gets a list of the current prompt split into tokens.

            Returns
            ----------
                List[str]
                    tokens
            
            '''
            return list(Promise.Tokens.tokens.keys())

        @override
        def __enter__(self) -> Model.Invoker:
            '''
            Denotes you are entering a context window cented around applying interventions to the LLM processing of
            the specified prompt. 
            '''

            # New prompt invocation means the generation context is set back to the first generated token.
            Module.generation_idx = 0

            # Run the prompt through the empty grapgh model to reset all Modules and set their shapes to the correct sizes.
            inputs = self.model.run_graph(
                self.prompt, *self.args, **self.kwargs)
            # Gets the tokenized version of the prompt to QOL features.
            tokenized = [self.model.tokenizer.decode(
                token) for token in inputs['input_ids'][0]]
            Promise.set_tokens(tokenized)

            # If in an invocation context window, running components of the model should be seen as an AdHoc intervention,
            # not actually running the model.
            Module.adhoc_mode = True
            torch.set_grad_enabled(False)

            return self

        @override
        def __exit__(self, exc_type, exc_val, exc_tb) -> None:
            '''
            Denotes you are exiting a context window cented around applying interventions to the LLM processing of
            the specified prompt. 
            '''

            # Get compiled Promise data and store them in class attributes.
            execution_graph, promises = Promise.compile()
            Model.Invoker.execution_graphs.append(execution_graph)
            Model.Invoker.prompts.append(self.prompt)
            Model.Invoker.promises = {**promises, **Model.Invoker.promises}

            # Clear Promise to allow for new invocations.
            Promise.execution_graph.clear()

            # Batch_idx denotes the index of invocation for multiple invocation runs so increment.
            Module.batch_idx += 1

            # Were leaving the Promise generation context window.
            Module.adhoc_mode = False
            torch.set_grad_enabled(True)

        def next(self) -> None:
            '''
            Denotes the context window is moving to the next token generation for multiple token generation runs.
            '''
            
            Module.generation_idx += 1
            Promise.set_tokens([f'<P{Module.generation_idx}>'])

            Module.adhoc_mode = False

            self.model.run_graph('_', *self.args, **self.kwargs)

            Module.adhoc_mode = True

    @classmethod
    def clear(cls) -> None:
        '''
        Clears everything. To be called after model execution and promise fulfillment. 
        '''
        Model.Invoker.clear()
        Promise.clear()
        Intervention.clear()
        Module.batch_idx = 0

    def __init__(self, model_name_or_path: str) -> None:

        self.model_name_or_path = model_name_or_path

        # Use init_empty_weights to create graph i.e the specified model with no loaded parameters,
        # to use for finding shapes of Module inputs and outputs, as well as replacing torch.nn.Module
        # with our Module.
        with accelerate.init_empty_weights(include_buffers=True):

            self.graph, self.tokenizer = self.get_model()

            # Set immediate graph childen modules as Models children so sub-modules
            # can be accessed directly.
            for name, module in self.graph.named_children():

                # Wrap all modules in our Module class.
                module = Module.wrap(module)

                setattr(self.graph, name, module)
                setattr(self, name, module)

            self.init_graph()

        self.local_model = None
        self.output = None

    def __repr__(self) -> str:
        return repr(self.graph)

    def __call__(self, *args, device='server', **kwargs):

        if device == 'server':

            self.output = self.submit_to_server(*args, **kwargs)

        else:

            if self.local_model is None:

                self.local_model, _ = self.get_model()

                # After the model is ran for one generation, denote to Intervention that were moving to the next token generation.
                self.local_model.register_forward_hook(
                    lambda module, input, output: Intervention.increment())

            self.local_model = self.local_model.to(device)

            self.output = self.run_model(*args, **kwargs)

            return self.output

    def init_graph(self) -> None:
        '''
        Perform any needed actions on first creation of graph.
        '''

        # Set module_path attribute so Modules know their path.
        for name, module in self.graph.named_modules():

            module.module_path = name

    @torch.inference_mode()
    def run_graph(self, prompt: str, *args, **kwargs) -> BatchEncoding:

        inputs = self.tokenizer([prompt], return_tensors='pt').to('cpu')

        self.graph(*args, **inputs.copy().to('meta'), **kwargs)
        
        return inputs

    @torch.inference_mode()
    def run_model(self, *args, **kwargs) -> Union[GenerateOutput, torch.LongTensor]:

        execution_graphs, promises, prompts = Model.Invoker.compile()

        for execution_graph in execution_graphs:

            Intervention.from_execution_graph(execution_graph, promises)

        Tensor.to(self.local_model.device)

        Adhoc.model = self.local_model

        inputs = self.tokenizer(prompts, padding=True, return_tensors='pt').to(
            self.local_model.device)

        with baukit.TraceDict(self.local_model, Get.layers(), retain_output=False, edit_output=output_intervene):
            output = self.local_model.generate(*args, **inputs, **kwargs)

        for id in Copy.copies:
            Promise.promises[id].value = Intervention.interventions[id]._value

        Model.clear()

        return output

    def submit_to_server(self, prompts: list[str], *args, **kwargs):

        pass

    def get_model(self) -> Tuple[PreTrainedTokenizer, PreTrainedModel]:

        tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path)

        model = AutoModelForCausalLM.from_pretrained(
            self.model_name_or_path, pad_token_id=tokenizer.eos_token_id)
        model.eval()

        tokenizer.pad_token = tokenizer.eos_token

        return model, tokenizer

    def invoke(self, prompt: str, *args, **kwargs) -> Model.Invoker:

        return Model.Invoker(self, prompt, *args, **kwargs)
    

