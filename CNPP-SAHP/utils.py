import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from model import get_non_pad_mask


def softplus(x, beta):
    # hard thresholding at 20
    temp = beta * x
    temp[temp > 20] = 20
    return 1.0 / beta * torch.log(1 + torch.exp(temp))


def compute_event(event, non_pad_mask):
    """ Log-likelihood of events. """

    # add 1e-9 in case some events have 0 likelihood
    event += math.pow(10, -9)
    event.masked_fill_(~non_pad_mask.bool(), 1.0)

    result = torch.log(event)
    return result


def compute_integral_biased(all_lambda, time, non_pad_mask):
    """ Log-likelihood of non-events, using linear interpolation. """

    diff_time = (time[:, 1:] - time[:, :-1]) * non_pad_mask[:, 1:]
    diff_lambda = (all_lambda[:, 1:] + all_lambda[:, :-1]) * non_pad_mask[:, 1:]

    biased_integral = diff_lambda * diff_time
    result = 0.5 * biased_integral
    return result


def compute_integral_unbiased(model, process_idx, data, time, non_pad_mask, type_mask):
    """ Log-likelihood of non-events, using Monte Carlo integration. """

    num_samples = 100

    diff_time = (time[:, 1:] - time[:, :-1]) * non_pad_mask[:, 1:]
    temp_time = diff_time.unsqueeze(2) * \
                torch.rand([*diff_time.size(), num_samples], device=data.device)
    temp_time /= (time[:, :-1] + 1).unsqueeze(2)

    temp_hid = model.linear_list[process_idx](data)[:, 1:, :]
    temp_hid = torch.sum(temp_hid * type_mask[:, 1:, :], dim=2, keepdim=True)

    all_lambda = softplus(temp_hid + model.alpha * temp_time, torch.abs(model.beta))

    print("all_lambda shape",all_lambda.shape)
    all_lambda = torch.sum(all_lambda, dim=2) / num_samples

    print("diff_time shape",diff_time.shape)
    print("all_lambda shape",all_lambda.shape)
    unbiased_integral = all_lambda * diff_time
    print("unbiased_integral shape", unbiased_integral.shape)
    return unbiased_integral


def log_likelihood(model, process_idx, data, time, types):
    """ Log-likelihood of sequence. """

    non_pad_mask = get_non_pad_mask(types).squeeze(2)

    num_types = model.num_types[process_idx]

    type_mask = torch.zeros((*types.size(), num_types)).to(data.device)
    for i in range(num_types):
        type_mask[:, :, i] = (types == i + 1).bool().to(data.device)

    all_hid = model.linear_list[process_idx](data)
    all_lambda = softplus(all_hid, torch.abs(model.beta))
    #print("log_likelihood",all_lambda)
    type_lambda = torch.sum(all_lambda * type_mask, dim=2)

    # event log-likelihood

    event_ll = compute_event(type_lambda, non_pad_mask)
    event_ll = torch.sum(event_ll, dim=-1)

    # non-event log-likelihood, either numerical integration or MC integration
    #non_event_ll = compute_integral_biased(type_lambda, time, non_pad_mask)
    non_event_ll = compute_integral_unbiased(model, process_idx, data, time, non_pad_mask, type_mask)
    non_event_ll = torch.sum(non_event_ll, dim=-1)
    print("non_event_ll shape",non_event_ll.shape)
    print("event_ll shape",event_ll.shape)

    return event_ll, non_event_ll


def type_loss(prediction, types, loss_func):
    """ Event prediction loss, cross entropy or label smoothing. """

    # convert [1,2,3] based types to [0,1,2]; also convert padding events to -1
    truth = types[:, 1:] - 1
    prediction = prediction[:, :-1, :]

    pred_type = torch.max(prediction, dim=-1)[1]
    # print("pred_type",pred_type.shape,pred_type)
    # print("truth", truth.shape, truth)
    correct_num = torch.sum(pred_type == truth)
    # print("correct_num",correct_num)
    # print(torch.sum(pred_type.view(-1)==truth.view(-1)))
    true_list=[]
    pred_list=[]
    for i in range(truth.shape[0]):
        pos_array = torch.nonzero(truth[i] == -1)
        if pos_array.shape[0] == 0:
            true_list.extend(list(truth[i].cpu().detach().numpy()))
            pred_list.extend(list(pred_type[i].cpu().detach().numpy()))
        else:
            #print("pos_array[0]",pos_array[0])
            true_list.extend(list(truth[i][:pos_array[0]].cpu().detach().numpy()))
            pred_list.extend(list(pred_type[i][:pos_array[0]].cpu().detach().numpy()))
    # compute cross entropy loss
    if isinstance(loss_func, LabelSmoothingLoss):
        loss = loss_func(prediction, truth)
    else:
        loss = loss_func(prediction.transpose(1, 2), truth)
    # print("type_loss loss",loss,"loss shape",loss.shape,"sum loss",torch.sum(loss))
    loss_r = torch.sum(loss)

    return loss_r, correct_num, true_list, pred_list


def time_loss(prediction, event_time):
    """ Time prediction loss. """

    prediction.squeeze_(-1)

    true = event_time[:, 1:] - event_time[:, :-1]
    prediction = prediction[:, :-1]

    # event time gap prediction
    diff = prediction - true
    se = torch.sum(diff * diff)
    return se


class LabelSmoothingLoss(nn.Module):
    """
    With label smoothing,
    KL-divergence between q_{smoothed ground truth prob.}(w)
    and p_{prob. computed by model}(w) is minimized.
    """

    def __init__(self, label_smoothing, tgt_vocab_size, ignore_index=-100):
        assert 0.0 < label_smoothing <= 1.0
        super(LabelSmoothingLoss, self).__init__()

        self.eps = label_smoothing
        self.num_classes = tgt_vocab_size
        self.ignore_index = ignore_index

    def forward(self, output, target):
        """
        output (FloatTensor): (batch_size) x n_classes
        target (LongTensor): batch_size
        """

        non_pad_mask = target.ne(self.ignore_index).float()

        target[target.eq(self.ignore_index)] = 0
        one_hot = F.one_hot(target, num_classes=self.num_classes).float()
        one_hot = one_hot * (1 - self.eps) + (1 - one_hot) * self.eps / self.num_classes

        log_prb = F.log_softmax(output, dim=-1)
        loss = -(one_hot * log_prb).sum(dim=-1)
        loss = loss * non_pad_mask
        return loss
