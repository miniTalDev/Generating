"""
---
title: Switch Transformer
summary: >
  This is an annotated implementation/tutorial a miniature version of Switch Transformer in PyTorch.
---

# Switch Transformer

This is a miniature implementation of the paper
[Switch Transformers: Scaling to Trillion Parameter Models with Simple and Efficient Sparsity](https://arxiv.org/abs/2101.03961).
Our implementation only has a few million parameters and doesn't do model parallel distributed training.
It does single GPU training but we implement the concept of switching as described in the paper.

The Switch Transformer is uses different parameters for each tokens by switching among parameters,
based on the token. So only a fraction of parameters is chosen for each token, so you
can have more parameters but a less computational cost.

The switching happens at the Position-wise Feedforward network (FFN) of of each transformer block.
Position-wise feedforward network is a two sequential fully connected layers.
In switch transformer we have multiple FFNs (multiple experts) and
we chose which one to use based on a router.
The outputs a set of probabilities for picking a FFN,
and we pick the one with highest probability and only evaluates that.
So essentially the computational cost is same as having a single FFN.
In our implementation this doesn't parallelize well when you have many or large FFNs since it's all
happening on a single GPU.
In a distributed setup you would have each FFN (each very large) on a different device.

The paper introduces another loss term to balance load among the experts (FFNs) and
discusses dropping tokens when routing is not balanced.

Here's a notebook for training a switch transformer on Tiny Shakespeare dataset.

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/lab-ml/nn/blob/master/labml_nn/transformers/feedback/experiment.ipynb)
[![View Run](https://img.shields.io/badge/labml-experiment-brightgreen)](https://web.lab-ml.com/run?uuid=d8eb9416530a11eb8fb50242ac1c0002)
"""

import torch
from torch import nn

from labml_helpers.module import Module
from labml_nn.transformers.mha import MultiHeadAttention
from labml_nn.transformers.models import FeedForward
from labml_nn.utils import clone_module_list


class SwitchFeedForward(Module):
    """
    ## Routing among multiple FFNs
    """

    def __init__(self, *,
                 capacity_factor: float,
                 drop_tokens: bool,
                 is_scale_prob: bool,
                 n_experts: int,
                 d_model: int,
                 d_ff: int,
                 dropout: float = 0.1):
        """
        * `capacity_factor` is the capacity of each expert as a factor relative to ideally balanced load
        * `drop_tokens` specifies whether to drop tokens if more tokens are routed to an expert than the capacity
        * `is_scale_prob` specifies whether to multiply the input to the FFN by the routing probability
        * `n_experts` is the number of experts
        * `d_model` is the number of features in a token embedding
        * `d_ff` is the number of features in the hidden layer of the FFN
        * `dropout` is dropout probability in the FFN
        """
        super().__init__()

        self.capacity_factor = capacity_factor
        self.is_scale_prob = is_scale_prob
        self.n_switches = n_experts
        self.drop_tokens = drop_tokens

        # FFN modules for each expert
        self.experts = nn.ModuleList([FeedForward(d_model, d_ff, dropout) for _ in range(n_experts)])
        # Routing layer and softmax
        self.switch = nn.Linear(d_model, n_experts)
        self.softmax = nn.Softmax(dim=-1)

    def __call__(self, x: torch.Tensor):
        seq_len, bs, d_model = x.shape
        x = x.view(-1, d_model)

        route_prob = self.softmax(self.switch(x))
        route_prob_max, routes = torch.max(route_prob, dim=-1)

        if self.is_scale_prob:
            factor = route_prob_max
        else:
            factor = route_prob_max / route_prob_max.detach()
        x = x * factor.view(-1, 1)

        # Get indexes of vectors going to each route
        indexes_list = [torch.eq(routes, i).nonzero(as_tuple=True)[0] for i in range(self.n_switches)]

        # Tensor to store outputs
        final_output = x.new_zeros(x.shape)

        # Capacity of a route
        capacity = int(self.capacity_factor * len(x) / self.n_switches)
        # Number of tokens going to each route
        counts = x.new_tensor([len(indexes_list[i]) for i in range(self.n_switches)])

        # Drop tokens
        dropped = []
        if self.drop_tokens:
            for i in range(self.n_switches):
                if len(indexes_list[i]) <= capacity:
                    continue
                indexes_list[i] = indexes_list[i][torch.randperm(len(indexes_list[i]))]
                dropped.append(indexes_list[i][capacity:])
                indexes_list[i] = indexes_list[i][:capacity]

        route_outputs = [self.experts[i](x[indexes_list[i], :]) for i in range(self.n_switches)]

        # Assign to final output
        for i in range(self.n_switches):
            final_output[indexes_list[i], :] = route_outputs[i]

        # Pass through the dropped tokens
        if dropped:
            dropped = torch.cat(dropped)
            final_output[dropped, :] = x[dropped, :]

        # Change the shape of the final output
        final_output = final_output.view(seq_len, bs, d_model)

        return final_output, counts, route_prob.sum(0), len(dropped)


class SwitchTransformerLayer(Module):
    def __init__(self, *,
                 d_model: int,
                 attn: MultiHeadAttention,
                 feed_forward: SwitchFeedForward,
                 dropout_prob: float):
        super().__init__()
        self.size = d_model
        self.attn = attn
        self.feed_forward = feed_forward
        self.dropout = nn.Dropout(dropout_prob)
        self.norm_self_attn = nn.LayerNorm([d_model])
        self.norm_ff = nn.LayerNorm([d_model])

    def __call__(self, *,
                 x: torch.Tensor,
                 mask: torch.Tensor):
        # Normalize the vectors before doing self attention
        z = self.norm_self_attn(x)
        # Run through self attention, i.e. keys and values are from self
        self_attn = self.attn(query=z, key=z, value=z, mask=mask)
        # Add the self attention results
        x = x + self.dropout(self_attn)

        # Normalize for feed-forward
        z = self.norm_ff(x)
        # Pass through the feed-forward network
        ff, counts, route_prob, n_dropped = self.feed_forward(z)
        # Add the feed-forward results back
        x = x + self.dropout(ff)

        return x, counts, route_prob, n_dropped


class SwitchTransformer(Module):
    """
    <a id="Encoder">
    ## Transformer Encoder
    </a>
    """

    def __init__(self, layer: SwitchTransformerLayer, n_layers: int):
        super().__init__()
        # Make copies of the transformer layer
        self.layers = clone_module_list(layer, n_layers)
        self.norm = nn.LayerNorm([layer.size])

    def __call__(self, x: torch.Tensor, mask: torch.Tensor):
        # Run through each transformer layer
        counts, route_prob, n_dropped = [], [], []
        for layer in self.layers:
            x, f, p, n_d = layer(x=x, mask=mask)
            counts.append(f)
            route_prob.append(p)
            n_dropped.append(n_d)
        # Finally, normalize the vectors
        return self.norm(x), torch.stack(counts), torch.stack(route_prob), n_dropped
