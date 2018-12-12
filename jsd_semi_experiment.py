import os
import sys
import pandas as pd
import logging
import warnings
import torch
import copy
import pathlib
import matplotlib.pyplot as plt

from absl import flags, app
from data.dataloader import ISICdata
from models.enet import Enet
from loss.loss import CrossEntropyLoss2d, JensenShannonDivergence
from utils.helpers import *
from tensorboardX import SummaryWriter
from torch.optim.lr_scheduler import MultiStepLR
from utils.logger import config_logger

logger = logging.getLogger(__name__)
logger.parent = None
warnings.filterwarnings('ignore')
global writer
global device


def get_default_parameter():
    flags.DEFINE_integer('num_workers', default=4, help='number of workers used in dataloader')
    flags.DEFINE_integer('batch_size', default=1, help='number of batch size')
    flags.DEFINE_boolean('semi_train__update_labeled', default=True,
                         help='update the labeled image while self training')
    flags.DEFINE_boolean('semi_train__update_unlabeled', default=True,
                         help='update the unlabeled image while self training')
    flags.DEFINE_boolean('run_pretrain', default=True,
                         help='run_pretrain')
    flags.DEFINE_boolean('run_semi', default=False,
                         help='run_self_training')
    flags.DEFINE_boolean('load_pretrain', default=True,
                         help='load_pretrain for self training')
    flags.DEFINE_string('model_path', default='checkpoints', help='path to the pretrained model')
    flags.DEFINE_string('loss_name', default='crossentropy', help='loss for semi supervised learning')
    flags.DEFINE_string('save_dir', default='demo_jsd/selftrain_0.04/use_ce', help='path to save')
    flags.DEFINE_float('labeled_percentate', default=1.0, help='how much percentage of labeled data you use')

    flags.DEFINE_integer('max_epoch', default=100, help='max_epoch for full training')
    flags.DEFINE_multi_integer('milestones', default=[20, 40, 60, 80, 100, 120, 140, 160, 180],
                               help='milestones for full training')
    flags.DEFINE_float('gamma', default=0.8, help='gamma for lr_scheduler in full training')
    flags.DEFINE_float('lr', default=0.001, help='lr for full training')
    flags.DEFINE_float('lr_decay', default=0.2, help='decay of learning rate schedule')
    flags.DEFINE_float('lamda', default=0.05, help='constant used during the training with unlabeled data')
    flags.DEFINE_multi_float('weight', default=[1, 1], help='weight balance for CE for full training')


def get_unlabeled_loss(lossname='crossentropy', device=None):
    if lossname == 'crossentropy':
        criterion = CrossEntropyLoss2d([1, 1])
        criterion.to(device)
    elif lossname == 'jsd':
        criterion = JensenShannonDivergence(reduce=True, size_average=False)
    else:
        raise NotImplementedError
    return criterion


def load_checkpoint(labeled_data, torchnets, path: list):
    lab_dataloaders = []
    import copy
    for path_i, net in zip(path, torchnets):
        labeled_data_ = copy.deepcopy(labeled_data)
        model = torch.load(path_i, map_location=lambda storage, loc: storage)
        logger.info('Saved_epoch: {}, Dice: {:3f}'.format(model['epoch'], model['dice']))
        labeled_data_.dataset.imgs = model['labeled_data'].dataset.imgs
        # correcting the length of gts from checkpoints
        labeled_data_.dataset.gts = model['labeled_data'].dataset.gts[:len(labeled_data_.dataset.imgs)]

        lab_dataloaders.append(labeled_data_)

    return lab_dataloaders


def batch_labeled_loss(img, mask, net, criterion):
    pred = net(img)
    labeled_loss = criterion(pred, mask.squeeze(1))
    ds = dice_loss(pred2segmentation(net(img)), mask.squeeze(1))
    return pred, labeled_loss, ds


def compute_pseudolabels(distributions: list):
    distributions = torch.cat([d.unsqueeze(0) for d in distributions], 0)
    return torch.mean(distributions, dim=0).max(1)[1]


def save_checkpoint(dices, dice_mv, nets, epoch, best_performance=False, name=None, save_dirs=''):
    for i, net in enumerate(nets):
        # save this checkpoint as last.pth
        dict2save = {}
        dict2save['epoch'] = epoch
        dict2save['dice'] = dices[i]
        dict2save['dice_mv'] = dice_mv
        dict2save['model'] = net.state_dict()
        if name is None:
            torch.save(dict2save, save_dirs[i].replace('_best.pth', '_last.pth'))
        # else:
        #     torch.save(dict2save, name + '/last.pth')

        if best_performance:
            dict2save = dict()
            dict2save['epoch'] = epoch
            dict2save['dice'] = dices[i]
            dict2save['dice_mv'] = dice_mv
            dict2save['model'] = net.state_dict()
            if name is None:
                torch.save(dict2save, save_dirs[i])
            # else:
            #     torch.save(dict2save, name + '/best.pth')
        else:
            return


def evaluate(epoch, nets, dataloader, best_dice_mv=-1, best=False, name=None, writer=None, mode='eval', savedirs=None,
             logger=None, device=None):
    with torch.no_grad():

        metrics = {}
        # for the labeled data
        dice1 = _evaluate(net=nets[0], dataloader=dataloader['labeled'][0], mode='eval', model_name='1',
                          alias='labeled', epoch=epoch, device=device)
        dice2 = _evaluate(net=nets[1], dataloader=dataloader['labeled'][1], mode='eval', model_name='2',
                          alias='labeled', epoch=epoch, device=device)
        dice3 = _evaluate(net=nets[2], dataloader=dataloader['labeled'][2], mode='eval', model_name='3',
                          alias='labeled', epoch=epoch, device=device)
        metrics['{}/labeled/enet_{}'.format(name, 0)] = dice1
        metrics['{}/labeled/enet_{}'.format(name, 1)] = dice2
        metrics['{}/labeled/enet_{}'.format(name, 2)] = dice3

        logger.info('at epoch: {:3d}, under {} mode, labeled_data dice: {:.3f}, {:.3f}, {:.3f}'.format(epoch,
                                                                                                       mode,
                                                                                                       dice1,
                                                                                                       dice2,
                                                                                                       dice3))
        # for unlabeled data
        dices = _evaluate_mm(nets, dataloader['unlabeled'], mode='eval', alias='unlabeled', device=device)
        for i, dice in enumerate(dices):
            metrics['{}/unlabeled/enet_{}'.format(name, i)] = dice

        logger.info('at epoch: {:3d}, under {} mode, unlabeled_data dice: {:.3f}, {:.3f}, {:.3f}'.format(epoch,
                                                                                                         mode,
                                                                                                         dices[0],
                                                                                                         dices[1],
                                                                                                         dices[2]))

        ## for val data
        dices = _evaluate_mm(nets, dataloader['val'], mode='eval', alias='labeled',  device=device)
        for i, dice in enumerate(dices):
            metrics['{}/val/enet_{}'.format(name, i)] = dice

        _, dice_mv = mv_test(nets, dataloader['val'], device=device)

        logger.info('at epoch: {:3d}, under {} mode, val_data dice: {:.3f}, {:.3f}, {:.3f} and mv {:.3f}'.format(epoch,
                                                                                                                 mode,
                                                                                                                 dices[
                                                                                                                     0],
                                                                                                                 dices[
                                                                                                                     1],
                                                                                                                 dices[
                                                                                                                     2],
                                                                                                                 dice_mv))
        metrics['{}/val/majority_voting'.format(name)] = dice_mv

        if dice_mv > best_dice_mv:
            best_dice_mv = dice_mv
            best_performance = True

        if mode == 'eval':
            writer.add_scalars(name, metrics, epoch)
            save_checkpoint(dices, dice_mv, nets, epoch, best_performance=best, save_dirs=savedirs)

        return best_dice_mv


def _evaluate_mm(nets, dataloader, mode, alias, device):
    return [_evaluate(net, dataloader, mode, model_name=i, alias=alias, device=device) for (net, i) in zip(nets, ('1', '2', '3'))]


def _show_mask(dataloader, img, gt, predict):

    img = np.array(dataloader.dataset.inverse_std(img.cpu().float()))
    assert img.shape.__len__() == 3
    assert img.shape[2] == 3
    gt = gt.squeeze()
    assert gt.shape.__len__() == 2
    fig = plt.figure(1)
    plt.imshow(img)
    plt.contour(gt.cpu(), colors='red')
    plt.contour(predict.cpu(), colors='green')
    return fig


def _evaluate(net, dataloader, mode='eval', model_name=None, alias=None, epoch=0, device=None):
    global writer
    showimgList = dataloader.dataset.imgs[:10]

    assert mode in ('eval', 'train')
    dice_meter = AverageValueMeter()
    if mode == 'eval':
        net.eval()
    else:
        net.train()

    with torch.no_grad():
        for i, (img, gt, (name, _)) in enumerate(dataloader):
            img, gt = img.to(device), gt.to(device)
            pred_logit = net(img)
            pred_mask = pred2segmentation(pred_logit)
            for j, _name in enumerate(name):
                if _name in showimgList:
                    fig = _show_mask(dataloader, img[j], gt[j], pred_mask[j])
                    writer.add_figure('%s/%s/%s' % (model_name, alias, os.path.basename(name[0])), fig, epoch)

            dice_meter.add(dice_loss(pred_mask, gt))
    if mode == 'eval':
        net.train()
    assert net.training == True
    return dice_meter.value()[0]


def mv_test(nets_, test_loader_, device):
    class_number = 2

    """
    This function performs the evaluation with the test set containing labeled images.
    """

    map_(lambda x: x.eval(), nets_)

    dice_meters_test = [AverageValueMeter(), AverageValueMeter(), AverageValueMeter()]
    mv_dice_score_meter = AverageValueMeter()

    with torch.no_grad():
        for i, (img, mask, _) in enumerate(test_loader_):

            (img, mask) = img.to(device), mask.to(device)
            distri = []
            for idx, net_i in enumerate(nets_):
                pred_test = nets_[idx](img)
                distri.append(pred_test)

                dice_test = dice_loss(pred2segmentation(pred_test), mask.squeeze(1))
                dice_meters_test[idx].add(dice_test)

            pseudolabels = compute_pseudolabels(distri)
            mv_dice_score = dice_loss(pseudolabels, mask.squeeze(1))
            mv_dice_score_meter.add(mv_dice_score)

    map_(lambda x: x.train(), nets_)

    return [dice_meters_test[idx] for idx in range(3)], mv_dice_score_meter.value()[0]


def compute_dice(input, target):
    # with torch.no_grad:
    if input.shape[1] != 1: input = input.max(1)[1]
    smooth = 1.

    iflat = input.view(input.size(0), -1)
    tflat = target.view(input.size(0), -1)
    intersection = (iflat * tflat).sum(1)

    return float(((2. * intersection + smooth).float() / (iflat.sum(1) + tflat.sum(1) + smooth).float()).mean())


def save_hparams(hparams, writername):
    hparams = copy.deepcopy(hparams)
    import pandas as pd
    message = ''
    message += '----------------- Options ---------------\n'
    for k, v in sorted(hparams.items()):
        comment = ''
        message += '{:>25}: {:<30}{}\n'.format(str(k), str(v), comment)
    message += '----------------- End -------------------'
    print(message)
    file_name = os.path.join(writername, 'opt.txt')
    with open(file_name, 'wt') as opt_file:
        opt_file.write(message)
        opt_file.write('\n')
    pd.Series(hparams).to_csv(os.path.join(writername, 'opt.csv'))


def train_ensemble(nets_: list, data_loaders, hparam, device):
    """
    This function performs the training of the pre-trained models with the labeled and unlabeled data.
    """

    best_dice_mv = -1
    best_performance = False
    global logger
    global writer
    optimizers = [torch.optim.Adam(nets_[0].parameters(), lr=hparam['lr'], weight_decay=hparam['lr_decay']),
                  torch.optim.Adam(nets_[1].parameters(), lr=hparam['lr'], weight_decay=hparam['lr_decay']),
                  torch.optim.Adam(nets_[2].parameters(), lr=hparam['lr'], weight_decay=hparam['lr_decay'])]

    schedulers = [MultiStepLR(optimizer=optimizers[0], milestones=hparam['milestones'], gamma=hparam['gamma']),
                  MultiStepLR(optimizer=optimizers[1], milestones=hparam['milestones'], gamma=hparam['gamma']),
                  MultiStepLR(optimizer=optimizers[2], milestones=hparam['milestones'], gamma=hparam['gamma'])]

    if hparam['save_dir'] is not None:
        writername = 'runs/' + hparam['save_dir']
    else:
        writername = 'runs/'

    if not os.path.exists(writername):
        pathlib.Path(writername).mkdir(parents=True, exist_ok=True)

    nets_path = [os.path.join(writername, 'enet_0_semi_best.pth'),
                 os.path.join(writername, 'enet_1_semi_best.pth'),
                 os.path.join(writername, 'enet_2_semi_best.pth')]

    writer = SummaryWriter(writername)
    save_hparams(hparam, writername)
    logger = config_logger(logger, writername)

    lcriterion = get_unlabeled_loss('crossentropy', device=device)
    if hparam['loss_name'] == 'crossentropy':
        unlcriterion = get_unlabeled_loss('crossentropy', device=device)
    elif hparam['loss_name'] == 'jsd':
        unlcriterion = get_unlabeled_loss('jsd')
    else:
        raise NotImplementedError

    logger.info("STARTING THE ENSEMBLE TRAINING!!!!")
    for epoch in range(hparam['max_epoch']):

        best_dice_mv = evaluate(epoch + 1, nets=nets_, dataloader=data_loaders, best_dice_mv=best_dice_mv,
                                best=best_performance, name='train', writer=writer, mode='eval', savedirs=nets_path,
                                logger=logger, device=device)
        # logger.info('epoch = {0:4d}/{1:4d} training baseline'.format(epoch, hparam['max_epoch']))

        # train with labeled data
        for _ in range(len(data_loaders['labeled'])):
            # train with labeled data
            llost_lst, prediction_lst, dice_score_lst = [], [], []
            for lab_loader, net_i in zip(data_loaders['labeled'], nets_):
                imgs, masks, _ = image_batch_generator(lab_loader, device=device)
                prediction, llost, dice_score = batch_labeled_loss(imgs, masks, net_i, lcriterion)
                llost_lst.append(llost)
                prediction_lst.append(prediction)

            # train with unlabeled data
            imgs, _, _ = image_batch_generator(data_loaders['unlabeled'], device=device)
            pseudolabel, unlab_preds = get_mv_based_labels(imgs, nets_)
            total_loss = []

            if hparam['loss_name'] == 'crossentropy':
                uloss_lst = [unlcriterion(unlab_pred, pseudolabel) for unlab_pred in unlab_preds]
                total_loss = [x + hparam['lamda'] * y for x, y in zip(llost_lst, uloss_lst)]
            elif hparam['loss_name'] == 'jsd':
                uloss_lst = unlcriterion(unlab_preds)
                total_loss = [x + uloss_lst for x in llost_lst]

            for idx in range(len(optimizers)):
                optimizers[idx].zero_grad()
                total_loss[idx].backward()
                optimizers[idx].step()
                schedulers[idx].step()


def run(argv):
    del argv

    hparam = flags.FLAGS.flag_values_dict()
    class_number = 2
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    ## networks and optimisers
    nets = [Enet(class_number),
            Enet(class_number),
            Enet(class_number)]

    nets = map_(lambda x: x.to(device), nets)

    nets_path = [os.path.join(hparam['model_path'], 'enet_0_best.pth'),
                 os.path.join(hparam['model_path'], 'enet_1_best.pth'),
                 os.path.join(hparam['model_path'], 'enet_2_best.pth')]

    root = "datasets/ISIC2018"
    labeled_data = ISICdata(root=root, model='labeled', mode='semi', transform=True,
                            dataAugment=False, equalize=False)
    unlabeled_data = ISICdata(root=root, model='unlabeled', mode='semi', transform=True,
                              dataAugment=False, equalize=False)

    # unlabeled_data.imgs = unlabeled_data.imgs[:20]
    # unlabeled_data.gts = unlabeled_data.gts[:20]
    val_data = ISICdata(root=root, model='val', mode='semi', transform=True,
                        dataAugment=False, equalize=False)
    # val_data.imgs = val_data.imgs[:20]
    # val_data.gts = val_data.gts[:20]

    unlabeled_loader_params = {'batch_size': hparam['batch_size'],
                               'shuffle': True,
                               'num_workers': hparam['num_workers'],
                               'pin_memory': True}
    val_loader_params = {'batch_size': hparam['batch_size'],
                         'shuffle': False,
                         'num_workers': hparam['num_workers'],
                         'pin_memory': True}

    labeled_loader = DataLoader(labeled_data, **unlabeled_loader_params)
    unlabeled_loader = DataLoader(unlabeled_data, **unlabeled_loader_params)
    val_loader = DataLoader(val_data, **val_loader_params)
    labeled_loader = load_checkpoint(labeled_loader, nets, nets_path)

    data_loaders = {'labeled': labeled_loader,
                    'unlabeled': unlabeled_loader,
                    'val': val_loader}

    map_(lambda x, y: [x.load_state_dict(torch.load(y, map_location=lambda storage, loc: storage)['model']), x.train()],
         nets,
         nets_path)

    train_ensemble(nets, data_loaders, hparam, device=device)


if __name__ == "__main__":
    get_default_parameter()
    app.run(run)
