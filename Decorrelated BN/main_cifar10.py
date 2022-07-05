'''Train CIFAR10 with PyTorch.'''
from __future__ import print_function
import os
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
import time
import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torchvision
import torchvision.transforms as transforms
import os
import argparse
import sys
cwd = os.getcwd()
model_dir = os.path.join(cwd, 'models')
sys.path.append(model_dir)
from models import *
from utils import progress_bar, format_time
from ZCANorm import *
from models.skew_symmetric_conv import skew_conv

def isnan(x):
    return x != x


parser = argparse.ArgumentParser(description='PyTorch CIFAR10 Training')
parser.add_argument('--norm', default='batchnorm', type=str, help='norm layer type')
parser.add_argument('--batch_size', default=128, type=int, help='batch size')
parser.add_argument('--lr', default=0.1, type=float, help='learning rate')
parser.add_argument('--resume', '-r', action='store_true', help='resume from checkpoint')
parser.add_argument('--nog', action='store_true', help='whether to apply nearest-orthogonal gradient (NOG)')
parser.add_argument('--olr', action='store_true', help='whether to apply optimal learning rate (OLR)')
parser.add_argument('--ow', action='store_true', help='whether to apply orthogonal convolutional weight')
args = parser.parse_args()

device = 'cuda' if torch.cuda.is_available() else 'cpu'
best_acc = 0  # best test accuracy
start_epoch = 0  # start from epoch 0 or last checkpoint epoch

# Data
print('==> Preparing data..')
transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

BatchSize = args.batch_size

# trainset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
trainset = torchvision.datasets.CIFAR10(root='/tmp', train=True, download=True, transform=transform_train)
trainloader = torch.utils.data.DataLoader(trainset, batch_size=BatchSize, shuffle=True, num_workers=2)

# testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
testset = torchvision.datasets.CIFAR10(root='/tmp', train=False, download=True, transform=transform_test)
testloader = torch.utils.data.DataLoader(testset, batch_size=BatchSize, shuffle=False, num_workers=2)

classes = ('plane', 'car', 'bird', 'cat', 'deer', 'dog', 'frog', 'horse', 'ship', 'truck')

# Model
norm = args.norm
print('==> Building model using {}..'.format(norm))
if norm == 'batchnorm':
    Norm = nn.BatchNorm2d
elif norm == 'mybatchnorm':
    Norm = BatchNorm
elif norm == 'zcanormbatch':
    Norm = ZCANormBatch


net = resnet18(Norm=Norm, num_classes=10)

save_dir = 'mpa_cifar10'
model_name = net._get_name()
id = randint(0, 1000)

logdir = os.path.join(save_dir, model_name+'18'+'group1', '{}-bs{}'.format(norm, BatchSize), str(id))
if not os.path.isdir(logdir):
    os.makedirs(logdir)

writer = SummaryWriter(log_dir=logdir)
print('RUNDIR: {}'.format(logdir))

net = net.to(device)
if device == 'cuda':
    net = torch.nn.DataParallel(net)
    cudnn.benchmark = True

#Change weight to orthogonal matrix if ow is applied
if args.ow:
    net.module.conv1 = skew_conv(3, 64, kernel_size=3, stride=1, padding=1, bias=False)

criterion = nn.CrossEntropyLoss()
#Define which layer to dynamically change the learning rate if olr is applied
if args.ow:
    my_list = ['conv1.random_conv_filter']
else:
    my_list = ['conv1.weight']
params = list(map(lambda x: x[1],list(filter(lambda kv: kv[0] in my_list, net.named_parameters()))))
base_params = list(map(lambda x: x[1],list(filter(lambda kv: kv[0] not in my_list, net.named_parameters()))))
optimizer = optim.SGD([{'params': params, 'lr': args.lr},{'params': base_params}], lr=args.lr, momentum=0.9, weight_decay=5e-4)

scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[100,200,300], gamma=0.1)

if args.resume:
    # Load checkpoint.
    print('==> Resuming from checkpoint..')
    assert os.path.isdir(logdir), 'Error: no checkpoint directory found!'
    checkpoint = torch.load('{}/best-{}-ckpt.t7'.format(logdir, model_name))
    net.load_state_dict(checkpoint['net'])
    best_acc = checkpoint['acc']
    start_epoch = checkpoint['epoch']

    scheduler.load_state_dict(checkpoint['scheduler'])
    optimizer.load_state_dict(checkpoint['optimizer'])

def train(epoch):
    print('\nEpoch: %d' % epoch)
    net.train()
    train_loss = 0
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        inputs, targets = inputs.to(device), targets.to(device)

        optimizer.zero_grad()
        outputs = net(inputs)

        loss = criterion(outputs, targets)

        loss.backward()
        # Apply Nearest Orthogonal Gradient Mapping
        if args.nog:
            if args.ow:
                weight_grad = net.module.conv1.random_conv_filter.grad
            else:
                weight_grad = net.module.conv1.weight.grad
            B, C, H, W = weight_grad.size()
            u, s, vh = torch.linalg.svd(weight_grad.view(B, C * H * W), full_matrices=False)
            s[:] = 1
            if args.ow:
                net.module.conv1.random_conv_filter.grad = u.mm(torch.diag(s)).mm(vh).view(B, C, H, W)
            else:
                net.module.conv1.weight.grad = u.mm(torch.diag(s)).mm(vh).view(B, C, H, W)
        # Apply Optimal Learnig Rate
        if args.olr:
            if args.ow:
                weight_grad = net.module.conv1.random_conv_filter.grad.view(-1, 1)
                weight = net.module.conv1.random_conv_filter.view(-1, 1)
            else:
                weight_grad = net.module.conv1.weight.grad.view(-1, 1)
                weight = net.module.conv1.weight.view(-1, 1)
            wtw = weight.t().mm(weight)
            ltw = weight_grad.t().mm(weight)
            ltl = weight_grad.t().mm(weight_grad)
            lr = (wtw * ltw) / (wtw * ltl + 2 * ltw * ltw)
            if lr > 0 and lr < optimizer.param_groups[1]['lr']:
                optimizer.param_groups[0]['lr'] = lr
            else:
                optimizer.param_groups[0]['lr'] = optimizer.param_groups[1]['lr']
        optimizer.step()

        if batch_idx % 500 == 0:
            writer.add_scalar('loss/train_loss', loss.item(), epoch*len(trainloader)+batch_idx+1)
        train_loss += loss.item()
        _, predicted = outputs.max(1)
        total += targets.size(0)
        correct += predicted.eq(targets).sum().item()

    acc = 100. * correct / total
    writer.add_scalar('loss/train_loss_avg', train_loss / len(trainloader), epoch)
    writer.add_scalar('train/accuracy', acc, epoch)
    writer.add_scalar('train/error', 100 - acc, epoch)
    print('current training error {}'.format(100-acc))


def test(epoch):
    global best_acc
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testloader):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = net(inputs)
            loss = criterion(outputs, targets)

            test_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()


    # Save checkpoint.
    acc = 100.*correct/total
    writer.add_scalar('loss/test_loss_avg', test_loss / len(testloader), epoch)
    writer.add_scalar('test/accuracy', acc, epoch)
    writer.add_scalar('test/error', 100-acc, epoch)
    print('Current Validation Error {}'.format(100 - acc))
    if acc > best_acc:
        print('Saving..')
        state = {
            'net': net.state_dict(),
            'acc': acc,
            'epoch': epoch,
            'optimizer': optimizer.state_dict(),
            'scheduler': scheduler.state_dict(),
        }
        torch.save(state, os.path.join(logdir, 'best-{}-ckpt.t7'.format(model_name)))
        best_acc = acc
    print('Best Validation Error {}'.format(100-best_acc))


for epoch in range(start_epoch, int(100*3.5)):
    train(epoch)
    scheduler.step()
    test(epoch)
