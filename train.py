# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import time
from alg.opt import *
from alg import alg, modelopera
from utils.util import set_random_seed, get_args, print_row, print_args, train_valid_target_eval_names, alg_loss_dict, print_environ
from datautil.getdataloader_single import get_act_dataloader
import torch
import torch.nn as nn

# === GNN imports ===
from models.gnn_extractor import TemporalGCN, build_correlation_graph
from diversify.utils.params import gnn_params
from shap_utils import GNNWrapper

# === SHAP imports ===
import shap
import numpy as np
import matplotlib.pyplot as plt
from torch_geometric.data import Batch

def explain_gnn_with_shap(gnn_model, data_loader, device, sample_count=5):
    gnn_model.eval()
    # Sample a batch from the loader
    batch = next(iter(data_loader))
    # Handle (data, label) tuple, or just data
    if isinstance(batch, (list, tuple)) and hasattr(batch[0], 'to_data_list'):
        data_list = batch[0].to_data_list()
    elif hasattr(batch, 'to_data_list'):
        data_list = batch.to_data_list()
    else:
        # Already a list of Data
        data_list = batch
    # Use a few graphs for background and explanation (KernelExplainer is slow)
    background = data_list[:sample_count]
    test_samples = data_list[:sample_count]

    wrapped_model = GNNWrapper(gnn_model)

    def gnn_predict(graph_list):
        # Accepts a list of Data objects, returns model output
        from torch_geometric.data import Batch
        batch = Batch.from_data_list(graph_list).to(device)
        out = wrapped_model(batch)
        if isinstance(out, torch.Tensor):
            out = out.detach().cpu().numpy()
        return out

    # KernelExplainer expects a callable and a list of Data objects
    explainer = shap.KernelExplainer(gnn_predict, background)
    shap_values = explainer.shap_values(test_samples)
    # For visualization, create a Batch for the test samples
    from torch_geometric.data import Batch
    graph_batch = Batch.from_data_list(test_samples)
    return shap_values, graph_batch

def plot_shap_summary(shap_values, graph_batch, feature_names=None):
    # Node features: graph_batch.x
    # Each row = node, columns = features (sensors)
    x_np = graph_batch.x.cpu().numpy()
    shap.summary_plot(shap_values, x_np, feature_names=feature_names, show=False)
    plt.tight_layout()
    plt.savefig("shap_summary.png")
    plt.show()

def main(args):
    s = print_args(args, [])
    set_random_seed(args.seed)

    print_environ()
    print(s)
    if args.latent_domain_num < 6:
        args.batch_size = 32*args.latent_domain_num
    else:
        args.batch_size = 16*args.latent_domain_num

    train_loader, train_loader_noshuffle, valid_loader, target_loader, _, _, _ = get_act_dataloader(args)

    best_valid_acc, target_acc = 0, 0

    algorithm_class = alg.get_algorithm_class(args.algorithm)
    algorithm = algorithm_class(args).cuda()
    algorithm.train()

    # ===== GNN feature extractor integration =====
    use_gnn = getattr(args, "use_gnn", 0)
    gnn = None
    if use_gnn:
        example_batch = next(iter(train_loader))[0] if hasattr(train_loader, '__iter__') else None
        in_channels = example_batch.shape[1] if example_batch is not None else 8
        gnn = TemporalGCN(
            in_channels=in_channels,
            hidden_dim=gnn_params["gcn_hidden_dim"],
            num_layers=gnn_params["gcn_num_layers"],
            lstm_hidden=gnn_params["lstm_hidden"],
            output_dim=gnn_params["feature_output_dim"]
        ).cuda()
        algorithm.featurizer = nn.Identity()
        print('[INFO] GNN feature extractor initialized. CNN featurizer is bypassed.')
        gnn_out_dim = gnn.out.out_features
        if hasattr(algorithm, "bottleneck"):
            algorithm.bottleneck = nn.Linear(gnn_out_dim, 256).cuda()
            print(f"[INFO] Bottleneck adjusted for GNN: {gnn_out_dim} -> 256")
        if hasattr(algorithm, "abottleneck"):
            algorithm.abottleneck = nn.Linear(gnn_out_dim, 256).cuda()
            print(f"[INFO] Adversarial bottleneck adjusted for GNN: {gnn_out_dim} -> 256")
        if hasattr(algorithm, "dbottleneck"):
            algorithm.dbottleneck = nn.Linear(gnn_out_dim, 256).cuda()
            print(f"[INFO] Domain bottleneck (dbottleneck) adjusted for GNN: {gnn_out_dim} -> 256")
        algorithm.gnn_extractor = gnn
        algorithm.use_gnn = True

    optd = get_optimizer(algorithm, args, nettype='Diversify-adv')
    opt = get_optimizer(algorithm, args, nettype='Diversify-cls')
    opta = get_optimizer(algorithm, args, nettype='Diversify-all')

    for round in range(args.max_epoch):
        print(f'\n========ROUND {round}========')
        print('====Feature update====')
        loss_list = ['class']
        print_row(['epoch']+[item+'_loss' for item in loss_list], colwidth=15)

        for step in range(args.local_epoch):
            for data in train_loader:
                if use_gnn and gnn is not None:
                    batch_x = data[0] if isinstance(data, (list, tuple)) else data
                    if len(batch_x.shape) == 4 and batch_x.shape[2] == 1:
                        batch_x = batch_x.squeeze(2)
                    gnn_graphs = build_correlation_graph(batch_x.cuda())
                    from torch_geometric.loader import DataLoader as GeoDataLoader
                    geo_loader = GeoDataLoader(gnn_graphs, batch_size=len(gnn_graphs))
                    for graph_batch in geo_loader:
                        graph_batch = graph_batch.cuda()
                        gnn_features = gnn(graph_batch)
                    if isinstance(data, (list, tuple)) and len(data) > 1:
                        data = (gnn_features, *data[1:])
                    else:
                        data = gnn_features
                loss_result_dict = algorithm.update_a(data, opta)
            print_row([step]+[loss_result_dict[item] for item in loss_list], colwidth=15)

        print('====Latent domain characterization====')
        loss_list = ['total', 'dis', 'ent']
        print_row(['epoch']+[item+'_loss' for item in loss_list], colwidth=15)

        for step in range(args.local_epoch):
            for data in train_loader:
                if use_gnn and gnn is not None:
                    batch_x = data[0] if isinstance(data, (list, tuple)) else data
                    if len(batch_x.shape) == 4 and batch_x.shape[2] == 1:
                        batch_x = batch_x.squeeze(2)
                    gnn_graphs = build_correlation_graph(batch_x.cuda())
                    from torch_geometric.loader import DataLoader as GeoDataLoader
                    geo_loader = GeoDataLoader(gnn_graphs, batch_size=len(gnn_graphs))
                    for graph_batch in geo_loader:
                        graph_batch = graph_batch.cuda()
                        gnn_features = gnn(graph_batch)
                    if isinstance(data, (list, tuple)) and len(data) > 1:
                        data = (gnn_features, *data[1:])
                    else:
                        data = gnn_features
                loss_result_dict = algorithm.update_d(data, optd)
            print_row([step]+[loss_result_dict[item] for item in loss_list], colwidth=15)

        algorithm.set_dlabel(train_loader)

        print('====Domain-invariant feature learning====')

        loss_list = alg_loss_dict(args)
        eval_dict = train_valid_target_eval_names(args)
        print_key = ['epoch']
        print_key.extend([item+'_loss' for item in loss_list])
        print_key.extend([item+'_acc' for item in eval_dict.keys()])
        print_key.append('total_cost_time')
        print_row(print_key, colwidth=15)

        sss = time.time()
        for step in range(args.local_epoch):
            for data in train_loader:
                if use_gnn and gnn is not None:
                    batch_x = data[0] if isinstance(data, (list, tuple)) else data
                    if len(batch_x.shape) == 4 and batch_x.shape[2] == 1:
                        batch_x = batch_x.squeeze(2)
                    gnn_graphs = build_correlation_graph(batch_x.cuda())
                    from torch_geometric.loader import DataLoader as GeoDataLoader
                    geo_loader = GeoDataLoader(gnn_graphs, batch_size=len(gnn_graphs))
                    for graph_batch in geo_loader:
                        graph_batch = graph_batch.cuda()
                        gnn_features = gnn(graph_batch)
                    if isinstance(data, (list, tuple)) and len(data) > 1:
                        data = (gnn_features, *data[1:])
                    else:
                        data = gnn_features
                step_vals = algorithm.update(data, opt)

            results = {
                'epoch': step,
            }

            results['train_acc'] = modelopera.accuracy(
                algorithm, train_loader_noshuffle, None)

            acc = modelopera.accuracy(algorithm, valid_loader, None)
            results['valid_acc'] = acc

            acc = modelopera.accuracy(algorithm, target_loader, None)
            results['target_acc'] = acc

            for key in loss_list:
                results[key+'_loss'] = step_vals[key]
            if results['valid_acc'] > best_valid_acc:
                best_valid_acc = results['valid_acc']
                target_acc = results['target_acc']
            results['total_cost_time'] = time.time()-sss
            print_row([results[key] for key in print_key], colwidth=15)

    print(f'Target acc: {target_acc:.4f}')

    # === SHAP explainability on GNN after training ===
    if use_gnn and gnn is not None:
        print("\n[INFO] Running SHAP explainability for GNN...")
        shap_values, graph_batch = explain_gnn_with_shap(gnn, valid_loader, device='cuda')
        feature_names = [f"Sensor {i}" for i in range(graph_batch.x.shape[1])]
        plot_shap_summary(shap_values, graph_batch, feature_names=feature_names)
        print("[INFO] SHAP summary saved to shap_summary.png")

if __name__ == '__main__':
    args = get_args()
    main(args)
