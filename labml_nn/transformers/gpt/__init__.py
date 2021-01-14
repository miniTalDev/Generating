"""
---
title: GPT
summary: >
  Implementation/tutorial of GPT model and training code.
---

# GPT

This is an tutorial of
[OpenAI GPT architecture](https://openai.com/blog/better-language-models/).
We got a bunch of implementation details from
[minGPT](https://github.com/karpathy/minGPT)
by [@karpathy](https://twitter.com/karpathy).
This implementation also uses character tiny shakespeare dataset.

GPT model is essentially a standard transformer with a few tweaks.
GPT-2 and especially GPT-3 models are quite large and won't fit on a
single GPU and will need model parallelism.
This implementation doesn't even use data parallelism and is intended to be
more of a tutorial.

Main differences of this to a standard autoregressive transformer
are the parameter initialization, weight decay, and learning rate schedule.
For the transformer we reuse the
[existing labml/nn transformer implementation](https://lab-ml.com/labml_nn/transformers/).
"""

import torch
from labml import experiment
from labml.configs import option
from labml_helpers.module import Module
from torch import nn

from labml_nn.experiments.nlp_autoregression import NLPAutoRegressionConfigs
from labml_nn.optimizers.configs import OptimizerConfigs
from labml_nn.transformers import TransformerConfigs, Encoder
from labml_nn.transformers.utils import subsequent_mask


class GPT(Module):
    """
    ## GPT model

    This consists of a token embedding layer, transformer encoder, and
    a final linear layer that gives token logits.
    """
    def __init__(self, encoder: Encoder, src_embed: Module, generator: Module):
        """
        * `encoder` is the transformer [Encoder](../models.html#Encoder)
        * `src_embed` is the token
        [embedding module (with positional encodings)](../models.html#EmbeddingsWithLearnedPositionalEncoding)
        * `generator` is the [final fully connected layer](../models.html#Generator) that gives the logits.
        """
        super().__init__()
        self.src_embed = src_embed
        self.encoder = encoder
        self.generator = generator

        # The mask will be initialized on the first call
        self.mask = None

    def __call__(self, x: torch.Tensor):
        # Create subsequent mask if mask is not initialized
        # or if the size of the mask is different
        if self.mask is None or self.mask.size(0) != len(x):
            # Subsequent mask, will mask out tokens from seeing future tokens
            self.mask = subsequent_mask(len(x)).to(x.device)
        # Get the token embeddings with positional encodings
        x = self.src_embed(x)
        # Transformer encoder
        x = self.encoder(x, self.mask)
        # Get logits
        x = self.generator(x)

        # Return results
        # (second value is for state, since our trainer is used with RNNs also)
        return x, None


class Configs(NLPAutoRegressionConfigs):
    """
    ## Configurations

    This inherits
    """
    model: GPT
    transformer: TransformerConfigs
    weight_decay: float = 0.1
    warmup_steps: int = 128 * 128 * 20

    optimizer = 'transformer_optimizer'


@option(Configs.transformer, 'GPT')
def _transformer_configs(c: Configs):
    conf = TransformerConfigs()
    conf.n_src_vocab = c.n_tokens
    conf.n_tgt_vocab = c.n_tokens
    conf.feed_forward_activation = 'GELU'

    return conf


def _init_weights(module):
    if isinstance(module, (nn.Linear, nn.Embedding)):
        module.weight.data.normal_(mean=0.0, std=0.02)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()
    elif isinstance(module, nn.LayerNorm):
        module.bias.data.zero_()
        module.weight.data.fill_(1.0)


@option(Configs.model)
def _model(c: Configs):
    m = GPT(c.transformer.encoder,
            c.transformer.src_embed,
            c.transformer.generator).to(c.device)

    m.apply(_init_weights)

    return m


@option(NLPAutoRegressionConfigs.optimizer)
def transformer_optimizer(c: NLPAutoRegressionConfigs):
    optimizer = OptimizerConfigs()

    decay = set()
    no_decay = set()
    whitelist_weight_modules = (nn.Linear,)
    blacklist_weight_modules = (nn.LayerNorm, nn.Embedding)
    for mn, m in c.model.named_modules():
        for pn, p in m.named_parameters():
            fpn = f'{mn}.{pn}' if mn else pn  # full param name

            if fpn.find('positional_encodings') != -1:
                no_decay.add(fpn)
            elif fpn.endswith('bias'):
                # all biases will not be decayed
                no_decay.add(fpn)
            elif fpn.endswith('weight'):
                if isinstance(m, whitelist_weight_modules):
                    # weights of whitelist modules will be weight decayed
                    decay.add(fpn)
                elif isinstance(m, blacklist_weight_modules):
                    # weights of blacklist modules will NOT be weight decayed
                    no_decay.add(fpn)

    # validate that we considered every parameter
    param_dict = {pn: p for pn, p in c.model.named_parameters()}

    inter_params = decay & no_decay
    if inter_params:
        raise ValueError("Repeated parameters", inter_params)

    missing_params = set(param_dict.keys()) - (decay | no_decay)
    if missing_params:
        raise ValueError('Missing parameters', missing_params)

    # create the pytorch optimizer object
    opt_groups = [
        {"params": [param_dict[pn] for pn in sorted(list(decay))], "weight_decay": c.weight_decay},
        {"params": [param_dict[pn] for pn in sorted(list(no_decay))], "weight_decay": 0.0},
    ]

    optimizer.parameters = opt_groups
    optimizer.optimizer = 'AdamWarmupCosineDecay'
    optimizer.d_model = c.d_model
    optimizer.weight_decay = c.weight_decay
    optimizer.learning_rate = 6e-4
    optimizer.betas = (0.9, 0.95)
    optimizer.eps = 1e-8
    optimizer.weight_decouple = True
    optimizer.total_steps = c.epochs * len(c.text.train)
    optimizer.warmup = c.warmup_steps // (c.batch_size * c.seq_len)

    return optimizer


def main():
    # Create experiment
    experiment.create(name="gpt")
    # Create configs
    conf = Configs()
    # Load configurations
    experiment.configs(conf,
                       # A dictionary of configurations to override
                       {'tokenizer': 'character',
                        'prompt_separator': '',
                        'prompt': 'It is ',
                        'text': 'tiny_shakespeare',

                        'seq_len': 128,
                        'epochs': 32,
                        'batch_size': 128,
                        'inner_iterations': 10,

                        # Transformer configurations
                        'transformer.d_model': 512,
                        'transformer.d_ff': 2048,
                        'transformer.n_heads': 8,
                        'transformer.n_layers': 6})

    # This is needed to initialize models
    conf.n_tokens = conf.text.n_tokens

    # Set models for saving and loading
    experiment.add_pytorch_models({'model': conf.model})

    # Start the experiment
    with experiment.start():
        # `TrainValidConfigs.run`
        conf.run()


if __name__ == '__main__':
    main()
