import Arena
from MCTS import MCTS
from gobang.GobangGame import GobangGame as OthelloGame
from gobang.pytorch.NNet import NNetWrapper as NNet
from gobang.GobangPlayers import *


import numpy as np
from utils import *

"""
use this script to play any two agents against each other, or play manually with
any agent.
"""

mini_othello = False  # Play in 6x6 instead of the normal 8x8.
human_vs_cpu = False


g = OthelloGame(9)

# all players
rp = RandomPlayer(g).play
gp = GreedyGobangPlayer(g).play
hp = HumanGobangPlayer(g).play



# nnet players
n1 = NNet(g)
# if mini_othello:
#     n1.load_checkpoint('./pretrained_models/othello/pytorch/','6x100x25_best.pth.tar')
# else:
#     n1.load_checkpoint('./pretrained_models/othello/pytorch/','8x8_100checkpoints_best.pth.tar')
n1.load_checkpoint('./temp/','checkpoint_20.pth.tar')
# n1.load_checkpoint('./result0715/','checkpoint_22.pth.tar')
args1 = dotdict({'numMCTSSims': 1000, 'cpuct':1.0, 'endGameRewardWeight': 5,})
mcts1 = MCTS(g, n1, args1)
n1p = lambda x: np.argmax(mcts1.getActionProb(x, temp=0))

if human_vs_cpu:
    player2 = hp
else:
    n2 = NNet(g)
    n2.load_checkpoint('./temp/','checkpoint_20.pth.tar')
    # n1.load_checkpoint('./result0715/','checkpoint_22.pth.tar')
    args2 = dotdict({'numMCTSSims': 20000, 'cpuct': 1.0, 'endGameRewardWeight': 5,})
    mcts2 = MCTS(g, n2, args2)
    n2p = lambda x: np.argmax(mcts2.getActionProb(x, temp=0))

    player2 = n2p  # Player 2 is neural network if it's cpu vs cpu.

arena = Arena.Arena(n1p, player2, g, display=OthelloGame.display)

print(arena.playGames(2, verbose=True))
