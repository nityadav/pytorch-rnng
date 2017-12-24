from typing import (Collection, List, Mapping, NamedTuple, Optional, Sequence, Sized, Tuple,
                    Union, cast)
from typing import Dict  # noqa

from nltk.tree import Tree
from torch.autograd import Variable
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init

from rnng.actions import Action, ShiftAction, ReduceAction, NTAction
from rnng.typing import Word, POSTag, NTLabel, WordId, POSId, NTId, ActionId


class EmptyStackError(Exception):
    def __init__(self):
        super().__init__('stack is already empty')


class StackLSTM(nn.Module, Sized):
    BATCH_SIZE = 1
    SEQ_LEN = 1

    def __init__(self,
                 input_size: int,
                 hidden_size: int,
                 num_layers: int = 1,
                 dropout: float = 0.,
                 lstm_class=None) -> None:
        if input_size <= 0:
            raise ValueError(f'nonpositive input size: {input_size}')
        if hidden_size <= 0:
            raise ValueError(f'nonpositive hidden size: {hidden_size}')
        if num_layers <= 0:
            raise ValueError(f'nonpositive number of layers: {num_layers}')
        if dropout < 0. or dropout >= 1.:
            raise ValueError(f'invalid dropout rate: {dropout}')

        if lstm_class is None:
            lstm_class = nn.LSTM

        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.lstm = lstm_class(input_size, hidden_size, num_layers=num_layers, dropout=dropout)
        self.h0 = nn.Parameter(torch.Tensor(num_layers, self.BATCH_SIZE, hidden_size))
        self.c0 = nn.Parameter(torch.Tensor(num_layers, self.BATCH_SIZE, hidden_size))
        init_states = (self.h0, self.c0)
        self._states_hist = [init_states]
        self._outputs_hist = []  # type: List[Variable]

        self.reset_parameters()

    def reset_parameters(self) -> None:
        for name, param in self.lstm.named_parameters():
            if name.startswith('weight'):
                init.orthogonal(param)
            else:
                assert name.startswith('bias')
                init.constant(param, 0.)
        init.constant(self.h0, 0.)
        init.constant(self.c0, 0.)

    def forward(self, inputs: Variable) -> Tuple[Variable, Variable]:
        if inputs.size() != (self.input_size,):
            raise ValueError(
                f'expected input to have size ({self.input_size},), got {tuple(inputs.size())}'
            )
        assert self._states_hist

        # Set seq_len and batch_size to 1
        inputs = inputs.view(self.SEQ_LEN, self.BATCH_SIZE, inputs.numel())
        next_outputs, next_states = self.lstm(inputs, self._states_hist[-1])
        self._states_hist.append(next_states)
        self._outputs_hist.append(next_outputs)
        return next_states

    def push(self, *args, **kwargs):
        return self(*args, **kwargs)

    def pop(self) -> Tuple[Variable, Variable]:
        if len(self._states_hist) > 1:
            self._outputs_hist.pop()
            return self._states_hist.pop()
        else:
            raise EmptyStackError()

    @property
    def top(self) -> Variable:
        # outputs: hidden_size
        return self._outputs_hist[-1].squeeze() if self._outputs_hist else None

    def __repr__(self) -> str:
        res = ('{}(input_size={input_size}, hidden_size={hidden_size}, '
               'num_layers={num_layers}, dropout={dropout})')
        return res.format(self.__class__.__name__, **self.__dict__)

    def __len__(self):
        return len(self._outputs_hist)


def log_softmax(inputs: Variable, restrictions: Optional[torch.LongTensor] = None) -> Variable:
    if restrictions is None:
        return F.log_softmax(inputs)

    if restrictions.dim() != 1:
        raise ValueError(f'restrictions must have dimension of 1, got {restrictions.dim()}')

    addend = Variable(
        inputs.data.new(inputs.size()).zero_().index_fill_(
            inputs.dim() - 1, restrictions, -float('inf')))
    return F.log_softmax(inputs + addend)


class StackElement(NamedTuple):
    subtree: Union[Word, Tree]
    emb: Variable
    is_open_nt: bool


class IllegalActionError(Exception):
    pass


class DiscRNNG(nn.Module):
    MAX_OPEN_NT = 100

    def __init__(self,
                 word2id: Mapping[Word, WordId],
                 pos2id: Mapping[POSTag, POSId],
                 nt2id: Mapping[NTLabel, NTId],
                 actionstr2id: Mapping[str, ActionId],
                 word_embedding_size: int = 32,
                 pos_embedding_size: int = 12,
                 nt_embedding_size: int = 60,
                 action_embedding_size: int = 16,
                 input_size: int = 128,
                 hidden_size: int = 128,
                 num_layers: int = 2,
                 dropout: float = 0.,
                 ) -> None:
        if str(ShiftAction()) not in actionstr2id:
            raise ValueError(f'no {ShiftAction()} action found in actionstr2id mapping')
        if str(ReduceAction()) not in actionstr2id:
            raise ValueError(f'no {ReduceAction()} action found in actionstr2id mapping')

        super().__init__()
        self.word2id = word2id
        self.pos2id = pos2id
        self.nt2id = nt2id
        self.actionstr2id = actionstr2id
        self.word_embedding_size = word_embedding_size
        self.pos_embedding_size = pos_embedding_size
        self.nt_embedding_size = nt_embedding_size
        self.action_embedding_size = action_embedding_size
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout

        # Parser states
        self._stack = []  # type: List[StackElement]
        self._buffer = []  # type: List[Word]
        self._history = []  # type: List[Action]
        self._num_open_nt = 0
        self._started = False

        # Embeddings
        self.word_embedding = nn.Embedding(self.num_words, self.word_embedding_size)
        self.pos_embedding = nn.Embedding(self.num_pos, self.pos_embedding_size)
        self.nt_embedding = nn.Embedding(self.num_nt, self.nt_embedding_size)
        self.action_embedding = nn.Embedding(self.num_actions, self.action_embedding_size)

        # Parser state encoders
        self.stack_encoder = StackLSTM(
            self.input_size, self.hidden_size, num_layers=self.num_layers, dropout=self.dropout
        )
        self.stack_guard = nn.Parameter(torch.Tensor(self.input_size))
        self.buffer_encoder = StackLSTM(
            self.input_size, self.hidden_size, num_layers=self.num_layers, dropout=self.dropout
        )
        self.buffer_guard = nn.Parameter(torch.Tensor(self.input_size))
        self.history_encoder = StackLSTM(
            self.input_size, self.hidden_size, num_layers=self.num_layers, dropout=self.dropout
        )
        self.history_guard = nn.Parameter(torch.Tensor(self.input_size))

        # Compositions
        self.fwd_composer = nn.LSTM(
            self.input_size, self.input_size, num_layers=self.num_layers, dropout=self.dropout
        )
        self.bwd_composer = nn.LSTM(
            self.input_size, self.input_size, num_layers=self.num_layers, dropout=self.dropout
        )

        # Transformations
        self.word2encoder = nn.Sequential(
            nn.Linear(self.word_embedding_size + self.pos_embedding_size, self.hidden_size),
            nn.ReLU(),
        )
        self.nt2encoder = nn.Sequential(
            nn.Linear(self.nt_embedding_size, self.hidden_size),
            nn.ReLU(),
        )
        self.action2encoder = nn.Sequential(
            nn.Linear(self.action_embedding_size, self.hidden_size),
            nn.ReLU(),
        )
        self.fwdbwd2composed = nn.Sequential(
            nn.Linear(2 * self.input_size, self.input_size),
            nn.ReLU(),
        )
        self.encoders2summary = nn.Sequential(
            nn.Dropout(self.dropout),
            nn.Linear(3 * self.hidden_size, self.hidden_size),
            nn.ReLU(),
        )
        self.summary2actionlogprobs = nn.Linear(self.hidden_size, self.num_actions)

        # Final embeddings
        self._word_emb = {}  # type: Dict[WordId, Variable]
        self._nt_emb = None  # type: Variable
        self._action_emb = None  # type: Variable

        self.reset_parameters()

    @property
    def num_words(self) -> int:
        return len(self.word2id)

    @property
    def num_pos(self) -> int:
        return len(self.pos2id)

    @property
    def num_nt(self) -> int:
        return len(self.nt2id)

    @property
    def num_actions(self) -> int:
        return len(self.actionstr2id)

    @property
    def stack_buffer(self) -> List[Union[Tree, Word]]:
        return [x.subtree for x in self._stack]

    @property
    def input_buffer(self) -> List[Word]:
        return list(reversed(self._buffer))

    @property
    def action_history(self) -> List[Action]:
        return list(self._history)

    @property
    def finished(self) -> bool:
        return (len(self._stack) == 1
                and not self._stack[0].is_open_nt
                and len(self._buffer) == 0)

    @property
    def started(self) -> bool:
        return self._started

    def reset_parameters(self) -> None:
        # Embeddings
        for name in 'word pos nt action'.split():
            embedding = getattr(self, f'{name}_embedding')
            embedding.reset_parameters()

        # Encoders
        for name in 'stack buffer history'.split():
            encoder = getattr(self, f'{name}_encoder')
            encoder.reset_parameters()

        # Compositions
        for name in 'fwd bwd'.split():
            lstm = getattr(self, f'{name}_composer')
            for pname, pval in lstm.named_parameters():
                if pname.startswith('weight'):
                    init.orthogonal(pval)
                else:
                    assert pname.startswith('bias')
                    init.constant(pval, 0.)

        # Transformations
        gain = init.calculate_gain('relu')
        for name in 'word nt action'.split():
            layer = getattr(self, f'{name}2encoder')
            init.xavier_uniform(layer[0].weight, gain=gain)
            init.constant(layer[0].bias, 1.)
        init.xavier_uniform(self.fwdbwd2composed[0].weight, gain=gain)
        init.constant(self.fwdbwd2composed[0].bias, 1.)
        init.xavier_uniform(self.encoders2summary[1].weight, gain=gain)
        init.constant(self.encoders2summary[1].bias, 1.)
        init.xavier_uniform(self.summary2actionlogprobs.weight)
        init.constant(self.summary2actionlogprobs.bias, 0.)

        # Guards
        for name in 'stack buffer history'.split():
            guard = getattr(self, f'{name}_guard')
            init.constant(guard, 0.)

    def start(self, words: Sequence[Word], pos_tags: Sequence[POSTag]) -> None:
        if len(words) != len(pos_tags):
            raise ValueError('words and POS tags must have equal length')
        if len(words) == 0:
            raise ValueError('words cannot be empty')

        self._stack = []
        self._buffer = []
        self._history = []
        self._num_open_nt = 0
        self._started = False

        while len(self.stack_encoder) > 0:
            self.stack_encoder.pop()
        while len(self.buffer_encoder) > 0:
            self.buffer_encoder.pop()
        while len(self.history_encoder) > 0:
            self.history_encoder.pop()

        # Feed guards as inputs
        self.stack_encoder.push(self.stack_guard)
        self.buffer_encoder.push(self.buffer_guard)
        self.history_encoder.push(self.history_guard)

        # Initialize input buffer and its LSTM encoder
        self._prepare_embeddings(words, pos_tags)
        for word in reversed(words):
            self._buffer.append(word)
            wid = self.word2id[word]
            assert wid in self._word_emb
            self.buffer_encoder.push(self._word_emb[wid])
        self._started = True

    def forward(self,
                words: Sequence[Word],
                pos_tags: Sequence[POSTag],
                actions: Sequence[Action]) -> Variable:
        self.start(words, pos_tags)
        llh = 0.
        for action in actions:
            log_probs = self.compute_action_log_probs()
            llh += log_probs[self.actionstr2id[str(action)]]
            try:
                action.execute_on(self)
            except IllegalActionError:
                break
        return -llh

    def decode(self, words: Sequence[Word], pos_tags: Sequence[POSTag]) -> List[ActionId]:
        self.start(words, pos_tags)
        id2actionstr = {v: k for k, v in self.actionstr2id.items()}
        best_action_ids = []
        while not self.finished:
            log_probs = self.compute_action_log_probs()
            max_a = torch.max(log_probs, dim=0)[1].data[0]
            best_action_ids.append(max_a)
            action = Action.from_string(id2actionstr[max_a])
            action.execute_on(self)
        return best_action_ids

    def push_nt(self, nonterm: NTLabel) -> None:
        action = NTAction(nonterm)
        self.verify_push_nt()
        self._push_nt(nonterm)
        self._append_history(action)

    def shift(self) -> None:
        self.verify_shift()
        self._shift()
        self._append_history(ShiftAction())

    def reduce(self) -> None:
        self.verify_reduce()
        self._reduce()
        self._append_history(ReduceAction())

    def verify_push_nt(self) -> None:
        self._verify_action()
        if len(self._buffer) == 0:
            raise IllegalActionError('cannot do NT(X) when input buffer is empty')
        if self._num_open_nt >= self.MAX_OPEN_NT:
            raise IllegalActionError('max number of open nonterminals reached')

    def verify_shift(self) -> None:
        self._verify_action()
        if len(self._buffer) == 0:
            raise IllegalActionError('cannot SHIFT when input buffer is empty')
        if self._num_open_nt == 0:
            raise IllegalActionError('cannot SHIFT when no open nonterminal exists')

    def verify_reduce(self) -> None:
        self._verify_action()
        last_is_nt = len(self._history) > 0 and isinstance(self._history[-1], NTAction)
        if last_is_nt:
            raise IllegalActionError(
                'cannot REDUCE when top of stack is an open nonterminal')
        if self._num_open_nt < 2 and len(self._buffer) > 0:
            raise IllegalActionError(
                'cannot REDUCE because there are words not SHIFT-ed yet')

    def _prepare_embeddings(self, words: Collection[Word], pos_tags: Collection[POSTag]):
        assert len(words) == len(pos_tags)

        word_ids = [self.word2id[w] for w in words]
        pos_ids = [self.pos2id[p] for p in pos_tags]
        nt_ids = list(range(self.num_nt))
        action_ids = list(range(self.num_actions))

        volatile = not self.training
        word_indices = Variable(self._new(word_ids).long().view(1, -1), volatile=volatile)
        pos_indices = Variable(self._new(pos_ids).long().view(1, -1), volatile=volatile)
        nt_indices = Variable(self._new(nt_ids).long().view(1, -1), volatile=volatile)
        action_indices = Variable(self._new(action_ids).long().view(1, -1), volatile=volatile)

        word_embs = self.word_embedding(word_indices).view(-1, self.word_embedding_size)
        pos_embs = self.pos_embedding(pos_indices).view(-1, self.pos_embedding_size)
        nt_embs = self.nt_embedding(nt_indices).view(-1, self.nt_embedding_size)
        action_embs = self.action_embedding(action_indices).view(-1, self.action_embedding_size)

        final_word_embs = self.word2encoder(torch.cat([word_embs, pos_embs], dim=1))
        final_nt_embs = self.nt2encoder(nt_embs)
        final_action_embs = self.action2encoder(action_embs)

        self._word_emb = dict(zip(word_ids, final_word_embs))
        self._nt_emb = final_nt_embs
        self._action_emb = final_action_embs

    def _verify_action(self) -> None:
        if not self._started:
            raise IllegalActionError('parser is not started yet, please call `start` first')
        if self.finished:
            raise IllegalActionError('cannot do action when parser is finished')

    def _append_history(self, action: Action) -> None:
        self._history.append(action)
        aid = self.actionstr2id[str(action)]
        assert isinstance(self._action_emb, Variable)
        self.history_encoder.push(self._action_emb[aid])

    def _push_nt(self, nonterm: NTLabel) -> None:
        nid = self.nt2id[nonterm]
        assert isinstance(self._nt_emb, Variable)
        self._stack.append(
            StackElement(Tree(nonterm, []), self._nt_emb[nid], True))
        self.stack_encoder.push(self._nt_emb[nid])
        self._num_open_nt += 1

    def _shift(self) -> None:
        assert len(self._buffer) > 0
        assert len(self.buffer_encoder) > 0
        word = self._buffer.pop()
        self.buffer_encoder.pop()
        wid = self.word2id[word]
        self._stack.append(StackElement(word, self._word_emb[wid], False))
        self.stack_encoder.push(self._word_emb[wid])

    def _reduce(self) -> None:
        children = []
        while len(self._stack) > 0 and not self._stack[-1].is_open_nt:
            children.append(self._stack.pop()[:-1])
        assert len(children) > 0
        assert len(self._stack) > 0

        children.reverse()
        child_subtrees, child_embs = zip(*children)
        open_nt = self._stack.pop()
        assert isinstance(open_nt.subtree, Tree)
        parent_subtree = cast(Tree, open_nt.subtree)
        parent_subtree.extend(child_subtrees)
        composed_emb = self._compose(open_nt.emb, child_embs)
        self._stack.append(StackElement(parent_subtree, composed_emb, False))
        self._num_open_nt -= 1
        assert self._num_open_nt >= 0

    def _compose(self, open_nt_emb: Variable, children_embs: Sequence[Variable]) -> Variable:
        assert open_nt_emb.size() == (self.input_size,)
        assert all(x.size() == (self.input_size,) for x in children_embs)

        fwd_input = [open_nt_emb]
        bwd_input = [open_nt_emb]
        for i in range(len(children_embs)):
            fwd_input.append(children_embs[i])
            bwd_input.append(children_embs[-i - 1])

        # (n_children + 1, 1, input_size)
        fwd_input = torch.stack(fwd_input).unsqueeze(1)
        bwd_input = torch.stack(bwd_input).unsqueeze(1)
        # (n_children + 1, 1, input_size)
        fwd_output, _ = self.fwd_composer(fwd_input)
        bwd_output, _ = self.bwd_composer(bwd_input)
        # (input_size,)
        fwd_emb = F.dropout(fwd_output[-1, 0], p=self.dropout, training=self.training)
        bwd_emb = F.dropout(bwd_output[-1, 0], p=self.dropout, training=self.training)
        # (input_size,)
        return self.fwdbwd2composed(torch.cat([fwd_emb, bwd_emb]).view(1, -1)).view(-1)

    def compute_action_log_probs(self):
        if not self._started:
            raise RuntimeError('parser is not started yet, please call `start` first')

        encoder_embs = [
            self.stack_encoder.top, self.buffer_encoder.top, self.history_encoder.top
        ]
        assert all(emb is not None for emb in encoder_embs)
        # (1, 3 * hidden_size)
        concatenated = torch.cat(encoder_embs).view(1, -1)
        # (1, hidden_size)
        parser_summary = self.encoders2summary(concatenated)
        illegal_action_ids = self._get_illegal_action_ids()
        # (num_actions,)
        return log_softmax(
            self.summary2actionlogprobs(parser_summary),
            restrictions=illegal_action_ids
        ).view(-1)

    def _get_illegal_action_ids(self) -> Optional[torch.LongTensor]:
        illegal_action_ids = [
            aid for astr, aid in self.actionstr2id.items() if not self._is_legal(astr)
        ]
        if not illegal_action_ids:
            return None
        return self._new(illegal_action_ids).long()

    def _is_legal(self, astr: str) -> bool:
        action = Action.from_string(astr)
        try:
            action.verify_on(self)
        except IllegalActionError:
            return False
        else:
            return True

    def _new(self, *args, **kwargs) -> torch.FloatTensor:
        return next(self.parameters()).data.new(*args, **kwargs)
