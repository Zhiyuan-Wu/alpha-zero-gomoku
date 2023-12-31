import logging
from random import shuffle
import numpy as np
from tqdm import tqdm
import torch
import torch.optim as optim
import torch.multiprocessing as mp
from torch.multiprocessing import RLock, Queue, Process, Value
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from gobang.pytorch.GobangNNet import GobangNNet as onnet
from gobang.pytorch.GobangNNet import loss_pi, loss_v, entropy
import time, datetime
from circular_dict import CircularDict
import os
from utils import *
from MCTS import MCTS, batch_MCTS

log = logging.getLogger(__name__)

def Sampler(identifier, q_job, q_ans, qdata, v_model, game, args, lock):
    num_workers = args.mcts_per_sampler
    mem_limit_bytes = int(args.available_mem_gb * 0.5 / args.sampler_num / 3 * 1024 * 1024 * 1024)
    shared_Ps = CircularDict(maxsize_bytes=int(mem_limit_bytes*2))
    shared_Es = CircularDict(maxsize_bytes=int(mem_limit_bytes*0.5))
    shared_Vs = CircularDict(maxsize_bytes=int(mem_limit_bytes*0.5))
    query_buffer = []
    worker_pool = {i: batch_MCTS(game,args,shared_Ps,shared_Es,shared_Vs,query_buffer,i) for i in range(num_workers)}
    trainExamples = []
    model_version = v_model.value
    game_counter = 0
    game_counter_old = 0
    start_time = time.time()
    gpu_time = 0
    t_bar = tqdm(total=100, desc=f'Sampler {identifier:{2}}', position=identifier+4, lock_args=(True, args.tqdm_wait_time))
    t_bar.set_lock(lock)

    while 1:
        # sample
        for _ in range(args.numMCTSSims):
            # extend
            for i in range(num_workers):
                worker_pool[i].extend()
            
            # query network
            _start_time = time.time()
            query_index, query_content, query_state_string = zip(*query_buffer)
            q_job.put(np.array(query_content))
            query_buffer.clear()
            pi, v = q_ans.get()
            _end_time = time.time()
            gpu_time += _end_time - _start_time
            
            # set result
            for j,s in enumerate(query_state_string):
                # Set Ps and Vs
                shared_Ps[s] = pi[j]
                if s not in shared_Vs:
                    valids = game.getValidMoves(query_content[j], 1)
                    shared_Vs[s] = valids
                else:
                    valids = shared_Vs[s]
                shared_Ps[s] = shared_Ps[s] * valids
                sum_Ps_s = np.sum(shared_Ps[s])
                if sum_Ps_s > 0:
                    shared_Ps[s] /= sum_Ps_s  # renormalize
                else:  
                    # log.error("All valid moves were masked, doing a workaround.")
                    shared_Ps[s] = shared_Ps[s] + valids
                    shared_Ps[s] /= np.sum(shared_Ps[s])
                
                # Set values
                worker_pool[query_index[j]].current_value = - (v[j][0]-v[j][1]+1e-4*v[j][2])

            # backprop
            for i in range(num_workers):
                worker_pool[i].backprop()

        # take action
        for i in range(num_workers):
            mcts = worker_pool[i]
            canonicalBoard = game.getCanonicalForm(mcts.board, mcts.player)
            s = game.stringRepresentation(canonicalBoard)
            counts = mcts.Nsa[s]

            _counts = np.exp((counts * 1.0 - np.max(counts)) * args.softmax_temp)
            counts_sum = np.sum(_counts)
            pi_lable = [x / counts_sum for x in _counts]

            if mcts.episodeStep < args.tempThreshold:
                counts_sum = np.sum(counts)
                pi_rollout = [x / counts_sum for x in counts]
            else:
                pi_rollout = pi_lable
                
            sym = game.getSymmetries(canonicalBoard, pi_lable)
            mcts.game_record += [(b, p, mcts.player) for b, p in sym]

            action = np.random.choice(len(pi_rollout), p=pi_rollout)
            mcts.board, mcts.player = game.getNextState(mcts.board, mcts.player, action)
            mcts.episodeStep += 1
            if not args.keep_mcts_after_move:
                mcts.Nsa.clear()
                mcts.Qsa.clear()
                mcts.Ns.clear()

            r = game.getGameEnded(mcts.board, mcts.player)
            if r != 0:
                trainExamples += [(x[0], x[1], (2 if r==0.0001 else (0 if x[2]==r else 1))) for x in mcts.game_record]
                worker_pool[i].reset()
                game_counter += 1

        # Send Data
        if len(trainExamples)>256:
            # log.debug(f"Sampler: send to trainer. current result_pool size {len(trainExamples)}")
            qdata.put(trainExamples)
            trainExamples = []

        # reset buffer upon new model
        if v_model.value > model_version:
            model_version = v_model.value
            log.debug(f"Sampler: reset mcts buffer for model {model_version}")
            # Reset all mcts? Todo: update Ps with new model?
            shared_Ps.clear() 

        # Update state
        gpu_ratio = gpu_time/(time.time()-start_time)
        random_search_depth = np.mean([worker_pool[i].total_search_depth/worker_pool[i].search_count for i in range(num_workers)])
        t_bar.set_postfix(game=game_counter,gpu=gpu_ratio,sd=random_search_depth)
        t_bar.update(game_counter-game_counter_old)
        if t_bar.n >= 100:
            t_bar.reset()
        game_counter_old = game_counter

def Evaluator(sampler_pool, v_model, gpu_id, game,args):
    ''' This function is the Evaluator process, that:
            - evluate queries from sampler
            - check new models from trainer
    '''
    model = onnet(game, args)
    model.cuda(gpu_id)
    model.eval()
    model_version = 0

    while 1:
        # load new model once avaliable
        if v_model.value > model_version:
            model_version = v_model.value
            map_location = f'cuda:{gpu_id}'
            _path = os.path.join(args.checkpoint, f"checkpoint_{model_version}.pth")
            state_dict = torch.load(_path, map_location=map_location)
            consume_prefix_in_state_dict_if_present(state_dict, 'module.')
            _block_num = len([0 for x in state_dict.keys() if x.startswith('conv_layers')]) // 4
            _num_channels = state_dict['conv1.bias'].shape[0]
            if _block_num != len(model.conv_layers)//2 or _num_channels != model.conv1.bias.shape[0]:
                del model
                args.__setattr__("block_num", _block_num)
                args.__setattr__("num_channels", _num_channels)
                model = onnet(game, args)
                model.cuda(gpu_id)
                model.eval()
            model.load_state_dict(state_dict)
            log.debug(f"Evaluator: pull new model {model_version}")

        # evaluate queries
        for i in sampler_pool:
            q_job = sampler_pool[i][0]
            q_ans = sampler_pool[i][1]
            if q_job.qsize()>0:
                job = q_job.get()
                # ans = nnet.predict(np.array(job))
                board = torch.FloatTensor(np.array(job, dtype=np.float32))
                board = board.contiguous().cuda(gpu_id)
                with torch.no_grad():
                    pi, v = model(board)
                pi = torch.exp(pi).data.cpu().numpy().squeeze().astype(np.float16)
                v = torch.softmax(v, -1).data.cpu().numpy().squeeze()
                ans = (pi, v)
                q_ans.put(ans)

def mpTrainer(rank, world_size, v_model, q_distributor, game, args, lock):
    os.environ['MASTER_ADDR'] = '127.0.0.1'
    os.environ['MASTER_PORT'] = str(args.port)
    dist.init_process_group("nccl", rank=rank, world_size=world_size)
    if rank==0:
        t_train = tqdm(total=1, desc='Training', position=3, lock_args=(True, args.tqdm_wait_time), leave=False)
        t_train.set_lock(lock)
    
    model = onnet(game, args)
    model.cuda(rank)
    ddp_model = DDP(model, device_ids=[rank])

    if v_model.value > 0:
        map_location = f'cuda:{rank}'
        _path = os.path.join(args.checkpoint, f"checkpoint_{v_model.value}.pth")
        state_dict = torch.load(_path, map_location=map_location)
        _block_num = len([0 for x in state_dict.keys() if x.startswith('module.conv_layers')]) // 4
        _num_channels = state_dict['module.conv1.bias'].shape[0]
        # Trainer will ignore pretrain model if its size mismatch current configuration
        if _block_num == len(model.conv_layers)//2 and _num_channels == model.conv1.bias.shape[0]:
            ddp_model.load_state_dict(state_dict)

    optimizer = optim.Adam(ddp_model.parameters(), lr=args.lr)
    ddp_model.train()

    episodeHistory = []
    data_update_time = time.time()
    last_train_time = data_update_time + 1
    train_iter_counter = 1
    start_time = time.time()
    
    while 1:
        # Check data 
        if q_distributor.qsize()>0:
            _buffer = []
            for _ in range(q_distributor.qsize()):
                _buffer += q_distributor.get()
            episodeHistory.append(_buffer)
            if len(episodeHistory) > args.numItersForTrainExamplesHistory:
                episodeHistory.pop(0)
            data_update_time = time.time()
        
        # Train model when new data arrive
        if last_train_time < data_update_time and len(episodeHistory) > args.leastTrainingWindow:
            examples = []
            for e in episodeHistory:
                examples.extend(e)
            shuffle(examples)

            dist.barrier()
            batch_count = int(len(examples) / args.batch_size)
            if rank==0:
                t_train.reset(total=batch_count*args.epochs)
            for epoch in range(args.epochs):
                pi_losses = AverageMeter()
                v_losses = AverageMeter()
                pi_entropy = AverageMeter()
                for i in range(batch_count): # do we need make batch_count consistent between trainers? or set a constant value
                    # sample_ids = np.random.randint(len(examples), size=args.batch_size)
                    # boards, pis, vs = list(zip(*[examples[i] for i in sample_ids]))
                    boards, pis, vs = list(zip(*(examples[i*args.batch_size: (i+1)*args.batch_size])))
                    boards = torch.from_numpy(np.array(boards).astype(np.float32))
                    target_pis = torch.from_numpy(np.array(pis))
                    target_vs = torch.from_numpy(np.array(vs).astype(np.int64))

                    # predict
                    if args.cuda:
                        boards, target_pis, target_vs = boards.contiguous().cuda(rank), target_pis.contiguous().cuda(rank), target_vs.contiguous().cuda(rank)

                    # compute output
                    out_pi, out_v = ddp_model(boards)
                    l_pi = loss_pi(target_pis, out_pi)
                    l_v = loss_v(target_vs, out_v)
                    l_e = entropy(out_pi)
                    total_loss = args.pi_loss_weight * l_pi + l_v

                    # record loss
                    pi_losses.update(l_pi.item(), boards.size(0))
                    v_losses.update(l_v.item(), boards.size(0))
                    pi_entropy.update(l_e.item(), boards.size(0))
                    if rank==0:
                        t_train.set_postfix(L_pi=pi_losses, L_v=v_losses)

                    # compute gradient and do SGD step
                    optimizer.zero_grad()
                    total_loss.backward()
                    optimizer.step()
                    if rank==0:
                        t_train.update(1)
            
            if rank==0:
                new_model_version = int(time.time())
                _path = os.path.join(args.checkpoint, f"checkpoint_{new_model_version}.pth")
                torch.save(ddp_model.state_dict(), _path)
                v_model.value = new_model_version
            
            last_train_time = time.time()
            train_iter_counter += 1

        if rank==0:
            t_train.set_postfix(time=datetime.timedelta(seconds=int(time.time()-start_time)))
        time.sleep(0.1)

def Arena(v_model_sample, v_model_train, gpu_id, game, args, lock):
    ''' This function is the Arena process, that:
        - run game between two models
        - push best model to evaluator
    '''
    args.__setattr__("numMCTSSims", 100) # Todo: parallize arena playoff to allow more search

    model_best = onnet(game, args)
    model_best.cuda(gpu_id)
    model_current = onnet(game, args)
    model_current.cuda(gpu_id)

    if v_model_sample.value > 0:
        map_location = f'cuda:{gpu_id}'
        _path = os.path.join(args.checkpoint, f"checkpoint_{v_model_sample.value}.pth")
        state_dict = torch.load(_path, map_location=map_location)
        consume_prefix_in_state_dict_if_present(state_dict, 'module.')
        _block_num = len([0 for x in state_dict.keys() if x.startswith('conv_layers')]) // 4
        _num_channels = state_dict['conv1.bias'].shape[0]
        if _block_num != len(model_best.conv_layers)//2 or _num_channels != model_best.conv1.bias.shape[0]:
            del model_best
            args.__setattr__("block_num", _block_num)
            args.__setattr__("num_channels", _num_channels)
            model_best = onnet(game, args)
            model_best.cuda(gpu_id)
            model_best.eval()
        model_best.load_state_dict(state_dict)

    if v_model_train.value > 0:
        map_location = f'cuda:{gpu_id}'
        _path = os.path.join(args.checkpoint, f"checkpoint_{v_model_train.value}.pth")
        state_dict = torch.load(_path, map_location=map_location)
        consume_prefix_in_state_dict_if_present(state_dict, 'module.')
        _block_num = len([0 for x in state_dict.keys() if x.startswith('conv_layers')]) // 4
        _num_channels = state_dict['conv1.bias'].shape[0]
        if _block_num != len(model_current.conv_layers)//2 or _num_channels != model_current.conv1.bias.shape[0]:
            del model_current
            args.__setattr__("block_num", _block_num)
            args.__setattr__("num_channels", _num_channels)
            model_current = onnet(game, args)
            model_current.cuda(gpu_id)
            model_current.eval()
        model_current.load_state_dict(state_dict)

    last_model = v_model_train.value
    t_status = tqdm(total=args.arenaCompare, desc='Arena', position=1, lock_args=(True, args.tqdm_wait_time))
    t_status.set_lock(lock)
    t_status.set_postfix(time=time.ctime())

    while 1:
        if v_model_train.value > last_model:
            map_location = f'cuda:{gpu_id}'
            _path = os.path.join(args.checkpoint, f"checkpoint_{v_model_train.value}.pth")
            state_dict = torch.load(_path, map_location=map_location)
            consume_prefix_in_state_dict_if_present(state_dict, 'module.')
            _block_num = len([0 for x in state_dict.keys() if x.startswith('conv_layers')]) // 4
            _num_channels = state_dict['conv1.bias'].shape[0]
            if _block_num != len(model_current.conv_layers)//2 or _num_channels != model_current.conv1.bias.shape[0]:
                del model_current
                args.__setattr__("block_num", _block_num)
                args.__setattr__("num_channels", _num_channels)
                model_current = onnet(game, args)
                model_current.cuda(gpu_id)
                model_current.eval()
            model_current.load_state_dict(state_dict)
            last_model = v_model_train.value
            t_status.reset(total=args.arenaCompare)

            score_pad = [0, 0, 0] # best_score : current_score : draw
            for i in range(args.arenaCompare):
                mcts_best = MCTS(game, model_best, args)
                mcts_current = MCTS(game, model_current, args)
                board = np.zeros((game.n, game.n), dtype=np.int8)
                cur_player = 1
                best_color = {0: 1, 1: -1}[i%2]
                current_color = {0: -1, 1: 1}[i%2]
                while 1:
                    _player = {best_color: mcts_best, current_color: mcts_current}[cur_player]
                    canonicalBoard = game.getCanonicalForm(board, cur_player)
                    pi = _player.getActionProb(canonicalBoard, 0)
                    action = np.random.choice(len(pi), p=pi)
                    board, cur_player = game.getNextState(board, cur_player, action)
                    r = game.getGameEnded(board, 0)
                    if r!=0:
                        score_pad[{best_color: 0, current_color: 1, 1e-4: 2}[r]] += 1
                        break
                t_status.update(1)
                t_status.set_postfix(r=f'{str(v_model_sample.value)} vs {str(last_model)}={score_pad[0]}:{score_pad[1]}:{score_pad[2]}')
                if (score_pad[1]+score_pad[2]/2) / args.arenaCompare > args.updateThreshold or (score_pad[0]+score_pad[2]/2) / args.arenaCompare > 1 - args.updateThreshold:
                    break
            
            current_win_rate = (score_pad[1]+score_pad[2]/2) / args.arenaCompare
            if current_win_rate > args.updateThreshold:
                map_location = f'cuda:{gpu_id}'
                _path = os.path.join(args.checkpoint, f"checkpoint_{last_model}.pth")
                state_dict = torch.load(_path, map_location=map_location)
                consume_prefix_in_state_dict_if_present(state_dict, 'module.')
                _block_num = len([0 for x in state_dict.keys() if x.startswith('conv_layers')]) // 4
                _num_channels = state_dict['conv1.bias'].shape[0]
                if _block_num != len(model_best.conv_layers)//2 or _num_channels != model_best.conv1.bias.shape[0]:
                    del model_best
                    args.__setattr__("block_num", _block_num)
                    args.__setattr__("num_channels", _num_channels)
                    model_best = onnet(game, args)
                    model_best.cuda(gpu_id)
                    model_best.eval()
                model_best.load_state_dict(state_dict)
                v_model_sample.value = last_model
        
        time.sleep(0.1)

def Controller(q_data, trainer_pool, v_model_sample, v_model_train, game, args, lock):
    ''' This function is the Controller process, that:
            - collect data from sampler
            - push data to trainer
    '''
    t_status = tqdm(total=args.numIters, desc='Status', position=0, lock_args=(True, args.tqdm_wait_time))
    t_status.set_lock(lock)
    t_sample = tqdm(total=args.episode_size, desc="Sampling", position=2, lock_args=(True, args.tqdm_wait_time))
    t_sample.set_lock(lock)

    sample_buffer = []
    data_counter = 0
    old_model = v_model_train.value
    iteration_counter = 0
    duplicated_data_ratio = -1
    start_time = time.time()
    
    while 1:
        # Collect data from samplers
        if q_data.qsize()>0:
            for _ in range(q_data.qsize()):
                data = q_data.get()
                sample_buffer += data
                log.debug(f"Trainer: recieved from sampler. data size {len(data)}, buffer size{len(sample_buffer)}")
                t_sample.update(len(data))

                # Distribute data to trainers
                if len(sample_buffer) > args.episode_size:
                    data_counter += len(sample_buffer)
                    duplicated_data_ratio = len(sample_buffer) / len(set([x[0].tobytes() for x in sample_buffer]))
                    split_size = len(sample_buffer) // len(args.gpu_trainner)
                    for i in range(len(args.gpu_trainner)):
                        trainer_pool[i].put(sample_buffer[split_size*i:split_size*(i+1)])
                    sample_buffer = []
                    t_sample.reset()

        if v_model_train.value > old_model:
            old_model = v_model_train.value
            iteration_counter += 1
            t_status.update(1)

        t_status.set_postfix(i=iteration_counter, 
                             b=str(v_model_sample.value % 1000), 
                             c=str(v_model_train.value % 1000), 
                             r=duplicated_data_ratio,
                             d=f"{data_counter//1000000}M", 
                             s=f"{data_counter/(time.time()-start_time)*60/1000 : .1f}k/min")
        time.sleep(0.1)

class mpCoach():
    def __init__(self, game, args):
        self.game = game
        self.args = args
        self.global_lock = RLock()

    def learn(self):
        if self.args.load_model:
            v_model_sample = Value('i', self.args.model_series_number)
            v_model_train = Value('i', self.args.model_series_number)
        else:
            v_model_sample = Value('i', -1)
            v_model_train = Value('i', -1)
        sampler_num = self.args.sampler_num
        evaluator_num = len(self.args.gpu_evaluator)
        trainer_num = len(self.args.gpu_trainner)
        sampler_pool = {}
        trainer_pool = {}

        q_collector = Queue()
        for i in range(sampler_num):
            q_job = Queue()
            q_ans = Queue()
            sampler = Process(target=Sampler, args=(i, q_job, q_ans, q_collector, v_model_sample, self.game, self.args, self.global_lock))
            sampler.start()
            sampler_pool[i] = [q_job, q_ans]
        
        for i in range(evaluator_num):
            sampler_pool_subset = {k: sampler_pool[k] for j,k in enumerate(sampler_pool) if j%evaluator_num==i}
            evaluator = Process(target=Evaluator, args=(sampler_pool_subset, v_model_sample, self.args.gpu_evaluator[i], self.game, self.args))
            evaluator.start()

        for i in range(trainer_num):
            q_distributor = Queue()
            trainer = Process(target=mpTrainer, args=(self.args.gpu_trainner[i], len(self.args.gpu_trainner), v_model_train, q_distributor, self.game, self.args, self.global_lock))
            trainer.start()
            trainer_pool[i] = q_distributor
        
        arena = Process(target=Arena, args=(v_model_sample, v_model_train, self.args.gpu_arena, self.game, self.args, self.global_lock))
        arena.start()
        controller = Process(target=Controller, args=(q_collector, trainer_pool, v_model_sample, v_model_train, self.game, self.args, self.global_lock))
        controller.start()
        controller.join()