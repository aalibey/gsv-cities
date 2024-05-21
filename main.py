import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import ModelCheckpoint
from torch.optim import lr_scheduler
import utils

from dataloaders.GSVCitiesDataloader import GSVCitiesDataModule
from models import helper

from src.configs.args import parse_args
from argparse import Namespace

def get_args() -> Namespace:
    parser = parse_args()
    args = parser.parse_args()
    return args

class VPRModel(pl.LightningModule):
    """This is the main model for Visual Place Recognition
    we use Pytorch Lightning for modularity purposes.
    """

    def __init__(self,
                #---- Backbone
                backbone_arch='resnet50',
                pretrained=True,
                layers_to_freeze=1,
                layers_to_crop=[],
                
                #---- Aggregator
                agg_arch='ConvAP', #CosPlace, NetVLAD, GeM, AVG
                agg_config={},
                
                #---- Train hyperparameters
                lr=0.03, 
                optimizer='sgd',
                weight_decay=1e-3,
                momentum=0.9,
                warmpup_steps=500,
                milestones=[5, 10, 15],
                lr_mult=0.3,
                
                #----- Loss
                loss_name='MultiSimilarityLoss', 
                miner_name='MultiSimilarityMiner', 
                miner_margin=0.1,
                faiss_gpu=False
                 ):
        super().__init__()
        self.encoder_arch = backbone_arch
        self.pretrained = pretrained
        self.layers_to_freeze = layers_to_freeze
        self.layers_to_crop = layers_to_crop

        self.agg_arch = agg_arch
        self.agg_config = agg_config

        self.lr = lr
        self.optimizer = optimizer
        self.weight_decay = weight_decay
        self.momentum = momentum
        self.warmpup_steps = warmpup_steps
        self.milestones = milestones
        self.lr_mult = lr_mult

        self.loss_name = loss_name
        self.miner_name = miner_name
        self.miner_margin = miner_margin
        
        self.save_hyperparameters() # write hyperparams into a file
        
        self.loss_fn = utils.get_loss(loss_name)
        self.miner = utils.get_miner(miner_name, miner_margin)
        self.batch_acc = [] # we will keep track of the % of trivial pairs/triplets at the loss level 

        self.faiss_gpu = faiss_gpu
        
        # ----------------------------------
        # get the backbone and the aggregator
        self.backbone = helper.get_backbone(backbone_arch, pretrained, layers_to_freeze, layers_to_crop)
        self.aggregator = helper.get_aggregator(agg_arch, agg_config)
        
    # the forward pass of the lightning model
    def forward(self, x):
        x = self.backbone(x)
        x = self.aggregator(x)
        return x
    
    # configure the optimizer 
    def configure_optimizers(self):
        if self.optimizer.lower() == 'sgd':
            optimizer = torch.optim.SGD(self.parameters(), 
                                        lr=self.lr, 
                                        weight_decay=self.weight_decay, 
                                        momentum=self.momentum)
        elif self.optimizer.lower() == 'adamw':
            optimizer = torch.optim.AdamW(self.parameters(), 
                                        lr=self.lr, 
                                        weight_decay=self.weight_decay)
        elif self.optimizer.lower() == 'adam':
            optimizer = torch.optim.AdamW(self.parameters(), 
                                        lr=self.lr, 
                                        weight_decay=self.weight_decay)
        else:
            raise ValueError(f'Optimizer {self.optimizer} has not been added to "configure_optimizers()"')
        scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=self.milestones, gamma=self.lr_mult)
        return [optimizer], [scheduler]
    
    # configure the optizer step, takes into account the warmup stage
    def optimizer_step(self,  epoch, batch_idx,
                        optimizer, optimizer_idx, optimizer_closure,
                        on_tpu, using_native_amp, using_lbfgs):
        # warm up lr
        if self.trainer.global_step < self.warmpup_steps:
            lr_scale = min(1., float(self.trainer.global_step + 1) / self.warmpup_steps)
            for pg in optimizer.param_groups:
                pg['lr'] = lr_scale * self.lr
        optimizer.step(closure=optimizer_closure)
        
    #  The loss function call (this method will be called at each training iteration)
    def loss_function(self, descriptors, labels):
        # we mine the pairs/triplets if there is an online mining strategy
        if self.miner is not None:
            miner_outputs = self.miner(descriptors, labels)
            loss = self.loss_fn(descriptors, labels, miner_outputs)
            
            # calculate the % of trivial pairs/triplets 
            # which do not contribute in the loss value
            nb_samples = descriptors.shape[0]
            nb_mined = len(set(miner_outputs[0].detach().cpu().numpy()))
            batch_acc = 1.0 - (nb_mined/nb_samples)

        else: # no online mining
            loss = self.loss_fn(descriptors, labels)
            batch_acc = 0.0
            if type(loss) == tuple: 
                # somes losses do the online mining inside (they don't need a miner objet), 
                # so they return the loss and the batch accuracy
                # for example, if you are developping a new loss function, you might be better
                # doing the online mining strategy inside the forward function of the loss class, 
                # and return a tuple containing the loss value and the batch_accuracy (the % of valid pairs or triplets)
                loss, batch_acc = loss

        # keep accuracy of every batch and later reset it at epoch start
        self.batch_acc.append(batch_acc)
        # log it
        self.log('b_acc', sum(self.batch_acc) /
                len(self.batch_acc), prog_bar=True, logger=True)
        return loss
    
    # This is the training step that's executed at each iteration
    def training_step(self, batch, batch_idx):
        places, labels = batch
        
        # Note that GSVCities yields places (each containing N images)
        # which means the dataloader will return a batch containing BS places
        BS, N, ch, h, w = places.shape
        
        # reshape places and labels
        images = places.view(BS*N, ch, h, w)
        labels = labels.view(-1)

        # Feed forward the batch to the model
        descriptors = self(images) # Here we are calling the method forward that we defined above
        loss = self.loss_function(descriptors, labels) # Call the loss_function we defined above
        
        self.log('loss', loss.item(), logger=True)
        return {'loss': loss}
    
    # This is called at the end of eatch training epoch
    def training_epoch_end(self, training_step_outputs):
        # we empty the batch_acc list for next epoch
        self.batch_acc = []

    # For validation, we will also iterate step by step over the validation set
    # this is the way Pytorch Lghtning is made. All about modularity, folks.
    def validation_step(self, batch, batch_idx, dataloader_idx=None):
        places, _ = batch
        # calculate descriptors
        descriptors = self(places)
        return descriptors.detach().cpu()
    
    def validation_epoch_end(self, val_step_outputs):
        """at the end of each validation epoch
        descriptors are returned in their order
        depending on how the validation dataset is implemented 
        for this project (MSLS val, Pittburg val), it is always references then queries.
        For example, if we have n references and m queries, we will get 
        the descriptors for each val_dataset in a list as follows: 
        [R1, R2, ..., Rn, Q1, Q2, ..., Qm]
        we then split it to references=[R1, R2, ..., Rn] and queries=[Q1, Q2, ..., Qm]
        to calculate recall@K using the ground truth provided.
        """
        dm = self.trainer.datamodule
        # The following line is a hack: if we have only one validation set, then
        # we need to put the outputs in a list (Pytorch Lightning does not do it presently)
        if len(dm.val_datasets)==1: # we need to put the outputs in a list
            val_step_outputs = [val_step_outputs]
        
        for i, (val_set_name, val_dataset) in enumerate(zip(dm.val_set_names, dm.val_datasets)):
            feats = torch.concat(val_step_outputs[i], dim=0)
            
            num_references = val_dataset.num_references
            num_queries = val_dataset.num_queries
            ground_truth = val_dataset.ground_truth
            
            # split to ref and queries    
            r_list = feats[ : num_references]
            q_list = feats[num_references : ]

            recalls_dict, predictions = utils.get_validation_recalls(r_list=r_list, 
                                                q_list=q_list,
                                                k_values=[1, 5, 10, 15, 20, 25],
                                                gt=ground_truth,
                                                print_results=True,
                                                dataset_name=val_set_name,
                                                faiss_gpu=self.faiss_gpu
                                                )
            del r_list, q_list, feats, num_references, ground_truth

            self.log(f'{val_set_name}/R1', recalls_dict[1], prog_bar=False, logger=True)
            self.log(f'{val_set_name}/R5', recalls_dict[5], prog_bar=False, logger=True)
            self.log(f'{val_set_name}/R10', recalls_dict[10], prog_bar=False, logger=True)
        print('\n\n')
            
            
if __name__ == '__main__':
    args = get_args()
    pl.utilities.seed.seed_everything(seed=1, workers=True)
    
    # the datamodule contains train and validation dataloaders,
    # refer to ./dataloader/GSVCitiesDataloader.py for details
    # if you want to train on specific cities, you can comment/uncomment
    # cities from the list TRAIN_CITIES
    datamodule = GSVCitiesDataModule(
        batch_size=args.batch_size,
        img_per_place=args.img_per_place,
        min_img_per_place=args.min_img_per_place,
        # cities=['London', 'Boston', 'Melbourne'], # you can sppecify cities here or in GSVCitiesDataloader.py
        shuffle_all=args.shuffle_all,
        random_sample_from_each_place=args.random_sample_from_each_place,
        image_size=args.image_size,
        num_workers=args.num_workers,
        show_data_stats=args.show_data_stats,
        val_set_names=args.val_set_names,
    )
    
    # examples of backbones
    # resnet18, resnet50, resnet101, resnet152,
    # resnext50_32x4d, resnext50_32x4d_swsl , resnext101_32x4d_swsl, resnext101_32x8d_swsl
    # efficientnet_b0, efficientnet_b1, efficientnet_b2
    # swinv2_base_window12to16_192to256_22kft1k
    model = VPRModel(
        #-------------------------------
        #---- Backbone architecture ----
        backbone_arch=args.backbone_arch.value,
        pretrained=args.pretrained,
        layers_to_freeze=args.layers_to_freeze,
        layers_to_crop=args.layers_to_crop, # 4 crops the last resnet layer, 3 crops the 3rd, ...etc
        
        #---------------------
        #---- Aggregator -----
        # agg_arch='CosPlace',
        # agg_config={'in_dim': 512,
        #             'out_dim': 512},
        # agg_arch='GeM',
        # agg_config={'p': 3},
        
        agg_arch=args.agg_arch,
        agg_config=args.agg_config,

        #-----------------------------------
        #---- Training hyperparameters -----
        #
        lr=args.lr, # 0.03 for sgd
        optimizer=args.optimizer.value, # sgd, adam or adamw
        weight_decay=args.weight_decay, # 0.001 for sgd or 0.0 for adam
        momentum=args.momentum,
        warmup_steps=args.warmup_steps,
        milestones=args.milestones,
        lr_mult=args.lr_mult,
        #---------------------------------
        #---- Training loss function -----
        # see utils.losses.py for more losses
        # example: ContrastiveLoss, TripletMarginLoss, MultiSimilarityLoss,
        # FastAPLoss, CircleLoss, SupConLoss,
        #
        loss_name=args.loss_name.value, # example: MultiSimilarityLoss
        miner_name=args.miner_name, # example: TripletMarginMiner, MultiSimilarityMiner, PairMarginMiner
        miner_margin=args.miner_margin,
        faiss_gpu=args.faiss_gpu
    )
    
    # model params saving using Pytorch Lightning
    # we save the best 3 models accoring to Recall@1 on pittsburg val
    checkpoint_cb = ModelCheckpoint(
        monitor=args.monitor,
        filename=args.filename,
        auto_insert_metric_name=args.auto_insert_metric_name,
        save_weights_only=args.save_weights_only,
        save_top_k=args.save_top_k,
        mode=args.mode,
    )

    
    #------------------
    # we instanciate a trainer
    trainer = pl.Trainer(
        accelerator= args.accelerator,
        devices=args.devices,
        default_root_dir=args.default_root_dir, # Tensorflow can be used to viz 
        num_sanity_val_steps= args.num_sanity_val_steps, # runs N validation steps before stating training
        precision= args.precision, # we use half precision to reduce  memory usage (and 2x speed on RTX)
        max_epochs= args.max_epochs, 
        check_val_every_n_epoch= args.check_val_every_n_epoch, # run validation every epoch
        reload_dataloaders_every_n_epochs= args.reload_dataloaders_every_n_epochs, # we reload the dataset to shuffle the order
        log_every_n_steps=args.log_every_n_steps, # log every n steps,
        fast_dev_run= args.fast_dev_run, # comment if you want to start training the network and saving checkpoints
        callbacks=[checkpoint_cb],# we run the checkpointing callback (you can add more)
    
    )

    # we call the trainer, and give it the model and the datamodule
    # now you see the modularity of Pytorch Lighning?
    trainer.fit(model=model, datamodule=datamodule)