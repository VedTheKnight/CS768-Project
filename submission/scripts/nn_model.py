from math import ceil
import torch
import torch.nn.functional as F
from torch_geometric.nn import GINConv, MLP, DenseGINConv
from torch_geometric.nn import global_add_pool
from torch_geometric.nn.resolver import activation_resolver
from torch_geometric.utils import to_dense_adj, to_dense_batch, dense_to_sparse

from scripts.pooling.softpool.simple_softpool import SimpleSoftPool
from scripts.pooling.gmt.gmt_pool import GMTPooling 
from scripts.pooling.specpool.spec_pool import SpecPool
from scripts.pooling.lcp.learnable_cluster_pool import LearnableClusterPool


class GIN_Pool_Net(torch.nn.Module):
    def __init__(self, 
                 in_channels,           # Size of node features
                 out_channels,          # Number of classes
                 num_layers_pre=1,      # Number of GIN layers before pooling
                 num_layers_post=1,     # Number of GIN layers after pooling
                 hidden_channels=64,    # Dimensionality of node embeddings
                 norm=True,             # Normalise Layers in the GIN MLP
                 activation='ELU',      # Activation of the MLP in GIN 
                 average_nodes=None,    # Needed for dense pooling methods
                 max_nodes=None,        # Needed for random pool
                 pooling=None,          # Pooling method
                 pool_ratio=0.1,        # Ratio = nodes_after_pool/nodes_before_pool
                 ):
        super(GIN_Pool_Net, self).__init__()
        
        self.num_layers_pre = num_layers_pre
        self.num_layers_post = num_layers_post
        self.hidden_channels = hidden_channels
        self.act = activation_resolver(activation)
        self.pooling = pooling
        self.pool_ratio = pool_ratio
  
        # Pre-pooling block            
        self.conv_layers_pre = torch.nn.ModuleList()
        for _ in range(num_layers_pre):
            mlp = MLP([in_channels, hidden_channels, hidden_channels], act=activation)
            self.conv_layers_pre.append(GINConv(nn=mlp, train_eps=False))
            in_channels = hidden_channels
                        
        # Pooling block
        pooled_nodes = ceil(pool_ratio * average_nodes)     
        if pooling == 'softpool':
            self.pool = SimpleSoftPool(hidden_channels, pooled_nodes)
        elif pooling == 'gmt':
            num_pooled_nodes = ceil(pool_ratio * average_nodes)
            self.pool = GMTPooling(hidden_channels, num_heads=4, num_queries=num_pooled_nodes)
        elif pooling=='specpool':
            self.pool = SpecPool(hidden_channels,ratio=pool_ratio)
        elif pooling == 'learnable-cluster':
            self.pool = LearnableClusterPool(hidden_channels, pooled_nodes)
        else:
            assert pooling==None
        
        # Post-pooling block
        self.conv_layers_post = torch.nn.ModuleList()
        for _ in range(num_layers_post):
            mlp = MLP([hidden_channels, hidden_channels, hidden_channels], act=activation, norm=None)
            if pooling == 'softpool':
                self.conv_layers_post.append(DenseGINConv(nn=mlp, train_eps=False))                
            else:
                self.conv_layers_post.append(GINConv(nn=mlp, train_eps=False))

        # Readout
        self.mlp = MLP([hidden_channels, hidden_channels, hidden_channels//2, out_channels], 
                        act=activation,
                        norm=None,
                        dropout=0.5)


    def reset_parameters(self):
        for (name, module) in self._modules.items():
            try:
                module.reset_parameters()
            except AttributeError:
                if name != 'act':
                    for x in module:
                        x.reset_parameters()


    def forward(self, data):
        x = data.x    
        adj = data.edge_index
        batch = data.batch

        ### pre-pooling block
        for layer in self.conv_layers_pre:  
            x = self.act(layer(x, adj))
    
        ### pooling block
        if self.pooling == 'specpool':
            x, adj, batch = self.pool(x, adj, batch)
        elif self.pooling == 'softpool':
            x, mask = to_dense_batch(x, batch)
            adj = to_dense_adj(adj, batch)
            x, adj, aux_loss = self.pool(x, adj, mask)
        elif self.pooling == 'gmt':
            x = self.pool(x, batch)  
            x = x.mean(dim=1)        
            aux_loss = 0
            x = self.mlp(x)
            return F.log_softmax(x, dim=-1), aux_loss
        elif self.pooling == 'learnable-cluster':
            x, mask = to_dense_batch(x, batch)
            adj = to_dense_adj(adj, batch)
            x, adj, aux_loss = self.pool(x, adj, mask)
            edge_index, _ = dense_to_sparse(adj)
            edge_index = edge_index.long()  
            batch = torch.arange(x.size(0), device=x.device).repeat_interleave(x.size(1))  
            x = x.view(-1, x.size(-1))  
            adj = edge_index 
        elif self.pooling==None:
            pass
        else:
            raise KeyError("Not an implemented pooling method")
                
        ### post-pooling block
        for layer in self.conv_layers_post:  
            x = self.act(layer(x, adj))

        ### readout
        if self.pooling == 'softpool':
            x = torch.sum(x, dim=1)
        else:
            x = global_add_pool(x, batch)
        x = self.mlp(x)
        
        if 'aux_loss' not in locals():
            aux_loss=0
        return F.log_softmax(x, dim=-1), aux_loss