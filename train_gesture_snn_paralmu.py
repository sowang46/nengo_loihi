from urllib.request import urlretrieve
import matplotlib.pyplot as plt
import nengo
from nengo.utils.filter_design import cont2discrete
import numpy as np
import tensorflow as tf
import nengo_dl
import os

# set seed to ensure this example is reproducible
seed = 0
tf.random.set_seed(seed)
np.random.seed(seed)

unroll_factor = 10
n_samp = 2400
n_pts = 500
n_stream = 5
n_step = int(np.ceil(n_pts/n_stream))
n_step = int(np.ceil(n_step/unroll_factor))*unroll_factor

n_chan = 6
n_label = 12

n_train = int(0.8*n_samp)
n_test = n_samp - n_train

x_data = np.zeros((n_samp,n_pts*n_chan))
y_data = np.zeros((n_samp,1))

# process the raw data
with open("gesture_real_12_2400.txt", "r")  as in_file:
    data_lines = in_file.readlines()

for i in range(n_samp):
    line = data_lines[i]
    items = np.array(line.split(" "))
    items = items.astype(int)
    
    x_data[i,:] = items[:-1]
    y_data[i] = items[-1]
x_data = x_data.reshape((n_samp,n_chan,n_pts)).swapaxes(1,2)
y_data = y_data.reshape((n_samp,1,1))

# first random permutation
perm = np.random.permutation(x_data.shape[0])
x_data = x_data[perm]
y_data = y_data[perm]

# divide the dataset into training and testing group
x_train = x_data[:n_train]
y_train = y_data[:n_train]
x_test = x_data[n_train:]
y_test = y_data[n_train:]

# data augmentation
aug_ratio = 4
smax = 50
x_train_aug = np.zeros((n_train*aug_ratio,n_pts,n_chan))
for i in range(n_train):
    ls = -(n_pts-1)
    rs = n_pts-1
    for j in range(n_chan):
        pidx = np.where(x_train[i,:,j]>0)[0]
        if pidx.shape[0] > 0:
            if ls < -pidx[0]:
                ls = -pidx[0]
            if rs > n_pts-1-pidx[-1]:
                rs = n_pts-1-pidx[-1]
    if ls < -smax:
        ls = -smax
    if rs > smax:
        rs = smax
    sran = np.concatenate((np.array(range(ls,0)), np.array(range(1,rs+1))), axis=0)
    sval = np.random.choice(sran, aug_ratio, replace=False)
    for j in range(aug_ratio):
        x_train_aug[(j-1)*n_train+i:(j-1)*n_train+i+1,:,:] = np.roll(x_train[i:i+1,:,:],sval[j],axis=1)

x_train = np.concatenate((x_train, x_train_aug), axis=0)
y_train = np.tile(y_train, (aug_ratio+1,1,1))
n_train = x_train.shape[0]

class LMUCell(nengo.Network):
    def __init__(self, hd, md, theta, xd, xn, **kwargs):
        super().__init__(**kwargs)

        # compute the A and B matrices according to the LMU's mathematical derivation
        # (see the paper for details)
        Q = np.arange(md, dtype=np.float64)
        R = (2 * Q + 1)[:, None] / theta
        j, i = np.meshgrid(Q, Q)

        A = np.where(i < j, -1, (-1.0) ** (i - j + 1)) * R
        B = (-1.0) ** Q[:, None] * R
        C = np.ones((1, md))
        D = np.zeros((1,))

        A, B, _, _, _ = cont2discrete((A, B, C, D), dt=1.0, method="zoh")

        with self:
            nengo_dl.configure_settings(trainable=None)

            # create objects corresponding to the x/u/m/h variables in the above diagram
            self.x = [nengo.Node(size_in=xn, label='Node_x%d'%i) for i in range(xd)]
            self.u = [nengo.Node(size_in=1, label='Node_u%d'%i) for i in range(xd)]
            # self.u = [nengo_dl.TensorNode(tf.nn.tanh, shape_in=(1,), pass_time=False) for _ in range(xd)]
            self.m = [nengo.Node(size_in=md, label='Node_m%d'%i) for i in range(xd)]
            self.h = nengo_dl.TensorNode(tf.nn.tanh, shape_in=(hd,), pass_time=False)
            # self.h = nengo.Ensemble(hd, 1, neuron_type=nengo.Tanh(tau_ref=1), gain=np.ones(hd), bias=np.zeros(hd)).neurons

            # compute u_t from the above diagram. we have removed e_h and e_m as they
            # are not needed in this task.
            conn_inp = [nengo.Connection(self.x[i], self.u[i], transform=np.ones((1,xn)), synapse=None) for i in range(xd)]
            # conn_mu = [nengo.Connection(self.m[i], self.u[i], transform=nengo_dl.dists.Glorot(), synapse=0) for i in range(xd)]
            # conn_hu = [nengo.Connection(self.h[i], self.u[i], transform=nengo_dl.dists.Glorot(), synapse=0) for i in range(xd)]

            # compute m_t
            # in this implementation we'll make A and B non-trainable, but they
            # could also be optimized in the same way as the other parameters.
            # note that setting synapse=0 (versus synapse=None) adds a one-timestep
            # delay, so we can think of any connections with synapse=0 as representing
            # value_{t-1}.
            conn_A = [nengo.Connection(self.m[i], self.m[i], transform=A, synapse=0) for i in range(xd)]
            for i in range(xd):
                self.config[conn_A[i]].trainable = False
            conn_B = [nengo.Connection(self.u[i], self.m[i], transform=B, synapse=None) for i in range(xd)]
            for i in range(xd):
                self.config[conn_B[i]].trainable = False

            # compute h_t
            for i in range(xd):
                nengo.Connection(
                    self.x[i], self.h, transform=nengo_dl.dists.Glorot(), synapse=None
                )
            nengo.Connection(
                self.h, self.h, transform=nengo_dl.dists.Glorot(), synapse=0
            )
            for i in range(xd):
                nengo.Connection(
                    self.m[i],
                    self.h,
                    transform=nengo_dl.dists.Glorot(),
                    synapse=None,
                )

with nengo.Network() as net:
    # remove some unnecessary features to speed up the training
    nengo_dl.configure_settings(
        trainable=None,
        stateful=True,
        keep_history=False,
    )

    n_inp = n_stream
    # input node
    inp = [nengo.Node(np.zeros(n_inp), label='Node_i%d'%i) for i in range(n_chan)]

    # lmu cell
    lmu = LMUCell(
        hd=100,
        md=50,
        theta=n_step,
        xd=n_chan,
        xn=n_inp
    )
    # net.config[lmu.h].trainable = False

    conn = [nengo.Connection(inp[i], lmu.x[i], synapse=None) for i in range(n_chan)]
    for i in range(n_chan):
        net.config[conn[i]].trainable = False

    # dense linear readout
    out = nengo.Node(size_in=n_label, label='Node_out')
    nengo.Connection(lmu.h, out, transform=nengo_dl.dists.Glorot(), synapse=None)

    # record output. note that we set keep_history=False above, so this will
    # only record the output on the last timestep (which is all we need
    # on this task)
    p = nengo.Probe(out)

do_training = False
n_epoch = 80
model_name = 'model_wiaug#4_memory#50_parallel#5'
if not os.path.exists('./weights/%s'%model_name):
    os.mkdir('./weights/%s'%model_name)

with nengo_dl.Simulator(net, minibatch_size=120, unroll_simulation=10) as sim:
    sim.compile(
        loss=tf.losses.SparseCategoricalCrossentropy(from_logits=True),
        optimizer=tf.optimizers.Adam(),
        metrics=["accuracy"],
    )
    sim.keras_model.summary()

    x_train_dict = {}
    for i in range(n_chan):
        # x_train_dict[inp[i]] = np.pad(x_train[:,:,i:i+1],((0,0),(0,n_step*n_stream-n_pts),(0,0))).reshape((n_train,-1,n_stream)).max(2)[:,:,None]
        x_train_dict[inp[i]] = np.pad(x_train[:,:,i:i+1],((0,0),(0,n_step*n_stream-n_pts),(0,0))).reshape((n_train,-1,n_stream))
    x_test_dict = {}
    for i in range(n_chan):
        # x_test_dict[inp[i]] = np.pad(x_test[:,:,i:i+1],((0,0),(0,n_step*n_stream-n_pts),(0,0))).reshape((n_test,-1,n_stream)).max(2)[:,:,None]
        x_test_dict[inp[i]] = np.pad(x_test[:,:,i:i+1],((0,0),(0,n_step*n_stream-n_pts),(0,0))).reshape((n_test,-1,n_stream))

    test_acc = sim.evaluate(x_test_dict, y_test, verbose=0)["probe_accuracy"]
    print(f"Initial test accuracy: {test_acc * 100:.2f}%")

    if do_training:
        for i_epoch in range(n_epoch):
            print("Epoch #%03d:"%i_epoch)
            sim.fit(x_train_dict, y_train, epochs=1, stateful=True)
            sim.save_params("./weights/%s/params_epoch#%03d"%(model_name,i_epoch))
            test_acc = sim.evaluate(x_test_dict, y_test, verbose=0)["probe_accuracy"]
            print(f"Test accuracy: {test_acc * 100:.2f}%")
        sim.save_params("./weights/%s/params_final"%model_name)
    else:
        sim.load_params("./weights/%s/params_epoch#070"%model_name)

    test_acc = sim.evaluate(x_test_dict, y_test, verbose=0)["probe_accuracy"]
    print(f"Final test accuracy: {test_acc * 100:.2f}%")
    # test_result = sim.predict(x_test_dict)
    # print(test_result[p][0])
