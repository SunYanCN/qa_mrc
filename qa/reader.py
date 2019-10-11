from common.util import get_default_device,load_json_config
from common.experiment import Experiment
import torch
import itertools
from bert.tokenization import BertTokenizer
from mrc.bert.util import  load_bert_rc_model,BertInputConverter
from dataloader.dureader import BertRCDataset
import pandas as pd
import numpy as np
#import tensorflow as tf
import os
import pickle


# convert rawfields to dict
def torchtext_batch_to_dictlist(batch):
    d = {}
    fields_names = list(batch.fields)
    d = { k:getattr(batch,k) for k in fields_names if not isinstance(getattr(batch,k),torch.Tensor)}
    l = pd.DataFrame(d).to_dict('records')
    return l




# start_probs/end_probs list :[prob1,prob2....]
def extract_answer_dp_linear(start_probs,end_probs):
    # max_start_pos[i] max_start_pos end in i to get max score
    N = len(start_probs)
    assert N>0
    max_start_pos = [0 for _ in range(N)]
    for i in range(1,N):
        prob1 = start_probs[max_start_pos[i-1]]
        prob2 = start_probs[i]
        if prob1 >= prob2:
            max_start_pos[i] = max_start_pos[i-1]
        else:
            max_start_pos[i] = i
    max_span = None
    max_score = -100000
    for i in range(N):
        score = start_probs[max_start_pos[i]]*end_probs[i]
        if score > max_score:
            max_span = (max_start_pos[i],i)
            max_score = score
    return  max_span,max_score


# start_probs/end_probs list :[prob1,prob2....]
def extract_answer_brute_force(start_probs,end_probs):
    passage_len = len(start_probs)
    best_start, best_end, max_prob = -1, -1, 0
    for start_idx in range(passage_len):
        for ans_len in range(passage_len):
            end_idx = start_idx + ans_len
            if end_idx >= passage_len:
                continue
            prob = start_probs[start_idx]*end_probs[end_idx]
            if prob > max_prob:
                best_start = start_idx
                best_end = end_idx
                max_prob = prob
    return (best_start,best_end),max_prob


class BertReader():
    def __init__(self,config,device=None,decode_policy='greedy'):
        self.config = config
        if device is None:
            self.device = get_default_device()
        bert_config_path = '%s/bert_config.json'%(config.BERT_SERIALIZATION_DIR)
        self.model = load_bert_rc_model( bert_config_path,config.MODEL_PATH,self.device)
        self.model.load_state_dict(torch.load(config.MODEL_PATH,map_location=self.device))
        self.model = self.model.to( self.device)
        self.model.eval()
        #bert-base-chinese
        self.tokenizer =  BertTokenizer('%s/vocab.txt'%(config.BERT_SERIALIZATION_DIR), do_lower_case=True)
        self.decode_policy = decode_policy
    # documents {'question':[{'passage':...,}]}
    def extract_answer(self,documents,batch_size=16):
        examples = []
        for question,passage_dict_list  in documents.items():
            for dct in passage_dict_list:
                examples.append()
                passage = dct['passage']
                examples.append({'question':question,'passage':passage})
        
        dataset  = BertRCDataset(examples,self.config.max_query_length,self.config.max_seq_length,mode='eval',device=self.device)
        iterator = dataset.make_batchiter(batch_size=batch_size)
        _preds = self.evaluate_on_batch(iterator)
        #for dct in _preds:
        #    question = dct['question']
        #    passage = dct['passage']
        #    passage_dct_l = documents[question]
        #    for d   in passage_dct_l:
        #        if passage == d['passage']:
        #            d.update({'span':dct['span'],'span_score':dct['span_score']})
        return _preds

    # record : list of dict  [ {field1:value1,field2:value2...}}]
    def evaluate_on_records(self,records):
        pass


    def evaluate_on_batch(self,iterator):
        with torch.no_grad():
            preds = []
            for  i,batch in enumerate(iterator):
                if i % 20 == 0:
                    print('evaluate on %d batch'%(i))
                start_probs, end_probs = self.model( batch.input_ids, token_type_ids= batch.segment_ids, attention_mask= batch.input_mask)
                batch_dct_list =  torchtext_batch_to_dictlist(batch)
                for j in range(len(start_probs)):
                    sb,eb = start_probs[j].unsqueeze(0), end_probs[j].unsqueeze(0)
                    span,score = self.find_best_span_from_probs(sb,eb,self.decode_policy)
                    score = score.item() #輸出的score不是機率 所以不會介於0~1之間
                    answer = self.extact_answer_from_span(batch.question[j],batch.passage[j],span)
                    batch_dct_list[j].update({'span':answer,'span_score':score})
                    preds.append(batch_dct_list[j])
        return  preds

    def find_best_span_from_probs(self,start_probs, end_probs,policy):
        def greedy():
            best_start, best_end, max_prob = -1, -1, 0
            prob_start, best_start = torch.max(start_probs, 1)
            prob_end, best_end = torch.max(end_probs, 1)
            num = 0
            while True:
                if num > 3:
                    break
                if best_end >= best_start:
                    break
                else:
                    start_probs[0][best_start], end_probs[0][best_end] = 0.0, 0.0 #寫得很髒....
                    prob_start, best_start = torch.max(start_probs, 1)
                    prob_end, best_end = torch.max(end_probs, 1)
                num += 1
            max_prob = prob_start * prob_end
            if best_start <= best_end:
                return (best_start, best_end), max_prob
            else:
                return (best_end, best_start), max_prob

        return extract_answer_dp_linear(start_probs[0],end_probs[0])

    def extact_answer_from_span(self,q,p,span):
        text = "$" + q + "\n" + p
        answer = text[span[0]:span[1]+1]
        return answer





class ReaderFactory():
    NAME2CLS = {'bert_reader':BertReader,'bidaf':None}
    def __init__(self):
        pass
    @classmethod
    def from_config_path(cls,path,**kwargs):
        config = load_json_config(path)
        return cls.from_config(config,**kwargs)
    @classmethod
    def from_config(cls,config,**kwargs):
        if not hasattr(config,'READER_CLASS'):
            _cls = cls.NAME2CLS[kwargs['READER_CLASS']]
        else:
            _cls = cls.NAME2CLS[config.RANKER_CLASS]
        if 'READER_CLASS' in kwargs:
            del kwargs['READER_CLASS']
        return _cls(config,**kwargs)
    @classmethod
    def from_exp_name(cls,exp_name,**kwargs):
        config =  Experiment(exp_name).config
        return cls.from_config(config,**kwargs)



class BidafReader():
     def __init__(self,config,device=None):
        import time
        from mrc.bidaf.layers.basic_rnn import rnn
        from mrc.bidaf.layers.match_layer import MatchLSTMLayer
        from mrc.bidaf.layers.match_layer import AttentionFlowMatchLayer
        from mrc.bidaf.layers.pointer_net import PointerNetDecoder
        self.config = config

        #from mrc.bidaf import vocab
        #import sys
        #sys.modules['vocab'] = vocab
#
        #with open(os.path.join(config.VOCAB_DIR, 'vocab.data'), 'rb') as fin:
        #    self.vocab = pickle.load(fin)
        #del sys.modules['vocab']
#
        #self.algo = config.ALGO
        #self.model_dir = config.MODEL_DIR
        #
        #self.hidden_size =  config.HIDDEN_SIZE
 #
        #self.use_dropout =   config.DROPOUT_PROB < 1
#
        ## length limit
        #self.max_p_num = config.MAX_PASSAGE_NUM
        #self.max_p_len = config.MAX_PASSAGE_LEN
        #self.max_q_len = config.MAX_Q_LEN
        #self.max_a_len = config.MAX_A_LEN


        sess_config = tf.ConfigProto()
        sess_config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=sess_config)
        self._build_graph()
        self.sess.run(tf.global_variables_initializer())
        #self.saver = tf.train.Saver()
        #self.restore(self.model_dir)


     def _build_graph(self):
        """
        Builds the computation graph with Tensorflow
        """
        #start_t = time.time()
        self._setup_placeholders()
        print('????')
        #self._embed()
        #self._encode()
        #self._match()
        #self._fuse()
        #self._decode()
        #print('Time to build graph: {} s'.format(time.time() - start_t))
        #self.all_params = tf.trainable_variables()
        #param_num = sum([np.prod(self.sess.run(tf.shape(v))) for v in self.all_params])
        #print('There are {} parameters in the model'.format(param_num))

     def _setup_placeholders(self):
        """
        Placeholders
        """
        self.p = tf.placeholder(tf.int32, [None, None])
        self.q = tf.placeholder(tf.int32, [None, None])
        self.p_length = tf.placeholder(tf.int32, [None])
        self.q_length = tf.placeholder(tf.int32, [None])
        self.start_label = tf.placeholder(tf.int32, [None])
        self.end_label = tf.placeholder(tf.int32, [None])
        self.dropout_keep_prob = tf.placeholder(tf.float32)

     def _embed(self):
        """
        The embedding layer, question and passage share embeddings
        """
        print('GGGGGGGGGGGGGG')
        print('%d,%d'%(self.vocab.size(), self.vocab.embed_dim))
        with tf.device('/cpu:0'), tf.variable_scope('word_embedding'):
            self.word_embeddings = tf.get_variable(
                'word_embeddings',
                shape=(self.vocab.size(), self.vocab.embed_dim),
                initializer=tf.constant_initializer(self.vocab.embeddings),
                trainable=True
            )
            self.p_emb = tf.nn.embedding_lookup(self.word_embeddings, self.p)
            self.q_emb = tf.nn.embedding_lookup(self.word_embeddings, self.q)

     def _encode(self):
        """
        Employs two Bi-LSTMs to encode passage and question separately
        """
        with tf.variable_scope('passage_encoding'):
            self.sep_p_encodes, _ = rnn('bi-lstm', self.p_emb, self.p_length, self.hidden_size)
        with tf.variable_scope('question_encoding'):
            self.sep_q_encodes, _ = rnn('bi-lstm', self.q_emb, self.q_length, self.hidden_size)
        if self.use_dropout:
            self.sep_p_encodes = tf.nn.dropout(self.sep_p_encodes, self.dropout_keep_prob)
            self.sep_q_encodes = tf.nn.dropout(self.sep_q_encodes, self.dropout_keep_prob)

     def _match(self):
        """
        The core of RC model, get the question-aware passage encoding with either BIDAF or MLSTM
        """
        if self.algo == 'MLSTM':
            match_layer = MatchLSTMLayer(self.hidden_size)
        elif self.algo == 'BIDAF':
            match_layer = AttentionFlowMatchLayer(self.hidden_size)
        else:
            raise NotImplementedError('The algorithm {} is not implemented.'.format(self.algo))
        self.match_p_encodes, _ = match_layer.match(self.sep_p_encodes, self.sep_q_encodes,
                                                    self.p_length, self.q_length)
        if self.use_dropout:
            self.match_p_encodes = tf.nn.dropout(self.match_p_encodes, self.dropout_keep_prob)

     def _fuse(self):
        """
        Employs Bi-LSTM again to fuse the context information after match layer
        """
        with tf.variable_scope('fusion'):
            self.fuse_p_encodes, _ = rnn('bi-lstm', self.match_p_encodes, self.p_length,
                                         self.hidden_size, layer_num=1)
            if self.use_dropout:
                self.fuse_p_encodes = tf.nn.dropout(self.fuse_p_encodes, self.dropout_keep_prob)

     def _decode(self):
        """
        Employs Pointer Network to get the the probs of each position
        to be the start or end of the predicted answer.
        Note that we concat the fuse_p_encodes for the passages in the same document.
        And since the encodes of queries in the same document is same, we select the first one.
        """
        with tf.variable_scope('same_question_concat'):
            batch_size = tf.shape(self.start_label)[0] #....  by this line model can knows the actually max_passage_len  , start_label length is not equal to passage_len in feeddict
            concat_passage_encodes = tf.reshape(
                self.fuse_p_encodes,
                [batch_size, -1, 2 * self.hidden_size]
            )
            no_dup_question_encodes = tf.reshape(
                self.sep_q_encodes,
                [batch_size, -1, tf.shape(self.sep_q_encodes)[1], 2 * self.hidden_size]
            )[0:, 0, 0:, 0:]
        decoder = PointerNetDecoder(self.hidden_size)
        self.start_probs, self.end_probs = decoder.decode(concat_passage_encodes,
                                                          no_dup_question_encodes)


     def restore(self, model_dir):
        """
        Restores the model into model_dir from model_prefix as the model indicator
        """
        self.saver.restore(self.sess, os.path.join(model_dir, 'BIDAF'))
        print('Model restored from {}'.format(model_dir))