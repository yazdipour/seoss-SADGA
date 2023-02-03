import collections
import itertools
import json
import os
import attr
import torch
import nltk
import numpy as np
from stanfordcorenlp import StanfordCoreNLP
from sadgasql.models import abstract_preproc
from sadgasql.models.encoder import spider_enc_modules
from sadgasql.utils import registry
from sadgasql.utils import serialization
from sadgasql.utils import vocab
from sadgasql.utils.spider_match_utils import (
    compute_schema_linking,
    compute_cell_value_linking,
    compute_cell_value_linking_bart,
)
from transformers import BertModel, BertTokenizer, BartModel, BartTokenizer
from sadgasql.resources import corenlp


@attr.s
class SpiderEncoderState:
    state = attr.ib()
    memory = attr.ib()
    question_memory = attr.ib()
    schema_memory = attr.ib()
    words = attr.ib()

    pointer_memories = attr.ib()
    pointer_maps = attr.ib()

    m2c_align_mat = attr.ib()
    m2t_align_mat = attr.ib()

    def find_word_occurrences(self, word):
        return [i for i, w in enumerate(self.words) if w == word]


@attr.s
class PreprocessedSchema:
    column_names = attr.ib(factory=list)
    table_names = attr.ib(factory=list)
    table_bounds = attr.ib(factory=list)
    column_to_table = attr.ib(factory=dict)
    table_to_columns = attr.ib(factory=dict)
    foreign_keys = attr.ib(factory=dict)
    foreign_keys_tables = attr.ib(factory=lambda: collections.defaultdict(set))
    primary_keys = attr.ib(factory=list)

    # only for bert version
    normalized_column_names = attr.ib(factory=list)
    normalized_table_names = attr.ib(factory=list)


def preprocess_schema_uncached(schema,
                               tokenize_func,
                               include_table_name_in_column,
                               fix_issue_16_primary_keys,
                               bert=False):
    """If it's bert, we also cache the normalized version of 
    question/column/table for schema linking"""
    r = PreprocessedSchema()

    if bert: assert not include_table_name_in_column

    last_table_id = None
    for i, column in enumerate(schema.columns):
        col_toks = tokenize_func(
            column.name, column.unsplit_name)

        # assert column.type in ["text", "number", "time", "boolean", "others"]
        type_tok = f'<type: {column.type}>'
        if bert:
            # for bert, we take the representation of the first word
            column_name = col_toks + [type_tok]
            r.normalized_column_names.append(Bertokens(col_toks))
        else:
            column_name = [type_tok] + col_toks

        if include_table_name_in_column:
            if column.table is None:
                table_name = ['<any-table>']
            else:
                table_name = tokenize_func(
                    column.table.name, column.table.unsplit_name)
            column_name += ['<table-sep>'] + table_name
        r.column_names.append(column_name)

        table_id = None if column.table is None else column.table.id
        r.column_to_table[str(i)] = table_id
        if table_id is not None:
            columns = r.table_to_columns.setdefault(str(table_id), [])
            columns.append(i)
        if last_table_id != table_id:
            r.table_bounds.append(i)
            last_table_id = table_id

        if column.foreign_key_for is not None:
            r.foreign_keys[str(column.id)] = column.foreign_key_for.id
            r.foreign_keys_tables[str(column.table.id)].add(column.foreign_key_for.table.id)

    r.table_bounds.append(len(schema.columns))
    assert len(r.table_bounds) == len(schema.tables) + 1

    for table in schema.tables:
        table_toks = tokenize_func(
            table.name, table.unsplit_name)
        r.table_names.append(table_toks)
        if bert:
            r.normalized_table_names.append(Bertokens(table_toks))
    last_table = schema.tables[-1]

    r.foreign_keys_tables = serialization.to_dict_with_sorted_values(r.foreign_keys_tables)
    r.primary_keys = [
        column.id
        for table in schema.tables
        for column in table.primary_keys
    ] if fix_issue_16_primary_keys else [
        column.id
        for column in last_table.primary_keys
        for table in schema.tables
    ]

    return r


class EncoderPreproc(abstract_preproc.AbstractPreproc):

    def __init__(
            self,
            save_path,
            min_freq=3,
            max_count=5000,
            include_table_name_in_column=True,
            word_emb=None,
            count_tokens_in_word_emb_for_vocab=False,
            fix_issue_16_primary_keys=False,
            compute_sc_link=False,
            compute_cv_link=False,
            db_path=None):
        if word_emb is None:
            self.word_emb = None
        else:
            self.word_emb = registry.construct('word_emb', word_emb)

        self.data_dir = os.path.join(save_path, 'enc')
        self.include_table_name_in_column = include_table_name_in_column
        self.count_tokens_in_word_emb_for_vocab = count_tokens_in_word_emb_for_vocab
        self.fix_issue_16_primary_keys = fix_issue_16_primary_keys
        self.compute_sc_link = compute_sc_link
        self.compute_cv_link = compute_cv_link
        self.texts = collections.defaultdict(list)
        self.db_path = db_path

        self.vocab_builder = vocab.VocabBuilder(min_freq, max_count)
        self.vocab_path = os.path.join(save_path, 'enc_vocab.json')
        self.vocab_word_freq_path = os.path.join(save_path, 'enc_word_freq.json')
        self.vocab = None
        self.counted_db_ids = set()
        self.preprocessed_schemas = {}

        self.stanfordcore_nlp = StanfordCoreNLP(os.path.abspath(
                os.path.join(os.path.dirname(__file__), '../../../third_party/stanford-corenlp-full-2018-10-05')))

    def validate_item(self, item, section):
        return True, None

    def add_item(self, item, section, validation_info):
        preprocessed = self.preprocess_item(item, validation_info)
        self.texts[section].append(preprocessed)

        if section == 'train':
            if item.schema.db_id in self.counted_db_ids:
                to_count = preprocessed['question']
            else:
                self.counted_db_ids.add(item.schema.db_id)
                to_count = itertools.chain(
                    preprocessed['question'],
                    *preprocessed['columns'],
                    *preprocessed['tables'])

            for token in to_count:
                if count_token := (
                    self.word_emb is None
                    or self.count_tokens_in_word_emb_for_vocab
                    or self.word_emb.lookup(token) is None
                ):
                    self.vocab_builder.add_word(token)

    def clear_items(self):
        self.texts = collections.defaultdict(list)

    def preprocess_item(self, item, validation_info):
        question, question_for_copying = self._tokenize_for_copying(item.text, item.orig['question'])
        preproc_schema = self._preprocess_schema(item.schema)
        if self.compute_sc_link:
            assert preproc_schema.column_names[0][0].startswith("<type:")
            column_names_without_types = [col[1:] for col in preproc_schema.column_names]
            sc_link = compute_schema_linking(question, column_names_without_types, preproc_schema.table_names)
        else:
            sc_link = {"q_col_match": {}, "q_tab_match": {}}

        if self.compute_cv_link:
            cv_link = compute_cell_value_linking(question, item.schema)
        else:
            cv_link = {"num_date_match": {}, "cell_match": {}}

        # dependency parsing
        dependency_tree = self.stanfordcore_nlp.dependency_parse(item.orig['question'])
        word_adj_tuple = []
        start_index = 0
        for index, tuple in enumerate(dependency_tree):
            if tuple[0] == 'ROOT':
                start_index = index
                continue
            word_adj_tuple.append((tuple[1] + start_index - 1, tuple[2] + start_index - 1))

        return {
            'raw_question': item.orig['question'],
            'question': question,
            'question_for_copying': question_for_copying,
            'db_id': item.schema.db_id,
            'sc_link': sc_link,
            'cv_link': cv_link,
            'columns': preproc_schema.column_names,
            'tables': preproc_schema.table_names,
            'table_bounds': preproc_schema.table_bounds,
            'column_to_table': preproc_schema.column_to_table,
            'table_to_columns': preproc_schema.table_to_columns,
            'foreign_keys': preproc_schema.foreign_keys,
            'foreign_keys_tables': preproc_schema.foreign_keys_tables,
            'primary_keys': preproc_schema.primary_keys,
            'word_adj_tuple': word_adj_tuple,
        }

    def _preprocess_schema(self, schema):
        if schema.db_id in self.preprocessed_schemas:
            return self.preprocessed_schemas[schema.db_id]
        result = preprocess_schema_uncached(schema, self._tokenize,
                                            self.include_table_name_in_column, self.fix_issue_16_primary_keys)
        self.preprocessed_schemas[schema.db_id] = result
        return result

    def _tokenize(self, presplit, unsplit):
        return self.word_emb.tokenize(unsplit) if self.word_emb else presplit

    def _tokenize_for_copying(self, presplit, unsplit):
        if self.word_emb:
            return self.word_emb.tokenize_for_copying(unsplit)
        return presplit, presplit

    def save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        self.vocab = self.vocab_builder.finish()
        print(f"{len(self.vocab)} words in vocab")
        self.vocab.save(self.vocab_path)
        self.vocab_builder.save(self.vocab_word_freq_path)

        for section, texts in self.texts.items():
            with open(os.path.join(self.data_dir, f'{section}.jsonl'), 'w') as f:
                for text in texts:
                    f.write(json.dumps(text) + '\n')

    def load(self):
        self.vocab = vocab.Vocab.load(self.vocab_path)
        self.vocab_builder.load(self.vocab_word_freq_path)

    def dataset(self, section):
        return [
            json.loads(line)
            for line in open(os.path.join(self.data_dir, f'{section}.jsonl'))
        ]


@registry.register('encoder', 'Encoder')
class Encoder(torch.nn.Module):
    batched = True
    Preproc = EncoderPreproc

    def __init__(
            self,
            device,
            preproc,
            word_emb_size=300,
            hidden_size=256,
            dropout=0.,
            question_encoder=('emb', 'bilstm'),
            column_encoder=('emb', 'bilstm'),
            table_encoder=('emb', 'bilstm'),
            update_config={},
            include_in_memory=('question', 'column', 'table'),
            top_k_learnable=0):
        super().__init__()
        self._device = device
        self.preproc = preproc

        self.vocab = preproc.vocab
        self.word_emb_size = word_emb_size
        self.hidden_size = hidden_size
        assert self.hidden_size % 2 == 0
        word_freq = self.preproc.vocab_builder.word_freq
        top_k_words = {_a[0] for _a in word_freq.most_common(top_k_learnable)}
        self.learnable_words = top_k_words

        self.include_in_memory = set(include_in_memory)
        self.dropout = dropout

        self.question_encoder = self._build_modules(question_encoder)
        self.column_encoder = self._build_modules(column_encoder)
        self.table_encoder = self._build_modules(table_encoder)

        self.encs_update = registry.instantiate(
            spider_enc_modules.StructureAwareGraphAggrUpdate,
            update_config,
            unused_keys={"name"},
            device=self._device,
            hidden_size=hidden_size,
        )

    def _build_modules(self, module_types):
        module_builder = {
            'emb': lambda: spider_enc_modules.LookupEmbeddings(
                self._device,
                self.vocab,
                self.preproc.word_emb,
                self.word_emb_size,
                self.learnable_words),
            'bilstm': lambda: spider_enc_modules.BiLSTM(
                input_size=self.word_emb_size,
                output_size=self.hidden_size,
                dropout=self.dropout,
                summarize=False),
            'bilstm-summarize': lambda: spider_enc_modules.BiLSTM(
                input_size=self.word_emb_size,
                output_size=self.hidden_size,
                dropout=self.dropout,
                summarize=True),
        }

        modules = [module_builder[module_type]() for module_type in module_types]
        return torch.nn.Sequential(*modules)

    def forward(self, descs):
        qs = [[desc['question']] for desc in descs]
        q_enc, _ = self.question_encoder(qs)

        c_enc, c_boundaries = self.column_encoder([desc['columns'] for desc in descs])
        column_pointer_maps = [
            {
                i: list(range(left, right))
                for i, (left, right) in enumerate(
                    zip(c_boundaries_for_item, c_boundaries_for_item[1:])
                )
            }
            for c_boundaries_for_item in c_boundaries
        ]

        t_enc, t_boundaries = self.table_encoder([desc['tables'] for desc in descs])
        table_pointer_maps = [
            {
                i: list(range(left, right))
                for i, (left, right) in enumerate(
                    zip(t_boundaries_for_item, t_boundaries_for_item[1:])
                )
            }
            for desc, t_boundaries_for_item in zip(descs, t_boundaries)
        ]

        result = []
        for batch_idx, desc in enumerate(descs):
            q_enc_new_item, c_enc_new_item, t_enc_new_item, align_mat_item = \
                    self.encs_update.forward_unbatched(
                    desc,
                    q_enc.select(batch_idx).unsqueeze(1),
                    c_enc.select(batch_idx).unsqueeze(1),
                    c_boundaries[batch_idx],
                    t_enc.select(batch_idx).unsqueeze(1),
                    t_boundaries[batch_idx])

            memory = []
            words_for_copying = []
            if 'question' in self.include_in_memory:
                memory.append(q_enc_new_item)
                if 'question_for_copying' in desc:
                    assert q_enc_new_item.shape[1] == len(desc['question_for_copying'])
                    words_for_copying += desc['question_for_copying']
                else:
                    words_for_copying += [''] * q_enc_new_item.shape[1]
            if 'column' in self.include_in_memory:
                memory.append(c_enc_new_item)
                words_for_copying += [''] * c_enc_new_item.shape[1]
            if 'table' in self.include_in_memory:
                memory.append(t_enc_new_item)
                words_for_copying += [''] * t_enc_new_item.shape[1]
            memory = torch.cat(memory, dim=1)

            result.append(SpiderEncoderState(
                state=None,
                memory=memory,
                question_memory=q_enc_new_item,
                schema_memory=torch.cat((c_enc_new_item, t_enc_new_item), dim=1),
                words=words_for_copying,
                pointer_memories={
                    'column': c_enc_new_item,
                    'table': torch.cat((c_enc_new_item, t_enc_new_item), dim=1),
                },
                pointer_maps={
                    'column': column_pointer_maps[batch_idx],
                    'table': table_pointer_maps[batch_idx],
                },
                m2c_align_mat=align_mat_item[0],
                m2t_align_mat=align_mat_item[1],
            ))
        return result


'''
BERT version
'''

class Bertokens:
    def __init__(self, pieces):
        self.pieces = pieces

        self.normalized_pieces = None
        self.idx_map = None

        self.normalize_toks()

    def normalize_toks(self):
        """
        If the token is not a word piece, then find its lemma
        If it is, combine pieces into a word, and then find its lemma
        E.g., a ##b ##c will be normalized as "abc", "", ""
        NOTE: this is only used for schema linking
        """
        self.startidx2pieces = {}
        self.pieces2startidx = {}
        cache_start = None
        for i, piece in enumerate(self.pieces + [""]):
            if piece.startswith("##"):
                if cache_start is None:
                    cache_start = i - 1

                self.pieces2startidx[i] = cache_start
                self.pieces2startidx[i - 1] = cache_start
            else:
                if cache_start is not None:
                    self.startidx2pieces[cache_start] = i
                cache_start = None
        assert cache_start is None

        # combine pieces, "abc", "", ""
        combined_word = {}
        for start, end in self.startidx2pieces.items():
            assert end - start < 9
            pieces = [self.pieces[start]] + [self.pieces[_id].strip("##") for _id in range(start + 1, end)]
            word = "".join(pieces)
            combined_word[start] = word

        # remove "", only keep "abc"
        idx_map = {}
        new_toks = []
        for i, piece in enumerate(self.pieces):
            if i in combined_word:
                idx_map[len(new_toks)] = i
                new_toks.append(combined_word[i])
            elif i not in self.pieces2startidx:
                idx_map[len(new_toks)] = i
                new_toks.append(piece)
        self.idx_map = idx_map

        # lemmatize "abc"
        normalized_toks = []
        for tok in new_toks:
            ann = corenlp.annotate(tok, annotators=['tokenize', 'ssplit', 'lemma'])
            lemmas = [tok.lemma.lower() for sent in ann.sentence for tok in sent.token]
            lemma_word = " ".join(lemmas)
            normalized_toks.append(lemma_word)

        self.normalized_pieces = normalized_toks

    def bert_schema_linking(self, columns, tables):
        question_tokens = self.normalized_pieces
        column_tokens = [c.normalized_pieces for c in columns]
        table_tokens = [t.normalized_pieces for t in tables]
        sc_link = compute_schema_linking(question_tokens, column_tokens, table_tokens)

        new_sc_link = {}
        for m_type in sc_link:
            _match = {}
            for ij_str in sc_link[m_type]:
                q_id_str, col_tab_id_str = ij_str.split(",")
                q_id, col_tab_id = int(q_id_str), int(col_tab_id_str)
                real_q_id = self.idx_map[q_id]
                _match[f"{real_q_id},{col_tab_id}"] = sc_link[m_type][ij_str]

            new_sc_link[m_type] = _match
        return new_sc_link


class EncoderPreproc4Bert(EncoderPreproc):

    def __init__(
            self,
            save_path,
            db_path,
            fix_issue_16_primary_keys=False,
            include_table_name_in_column=False,
            bert_version="bert-base-uncased",
            compute_sc_link=True,
            compute_cv_link=False):

        self.data_dir = os.path.join(save_path, 'enc')
        self.db_path = db_path
        self.texts = collections.defaultdict(list)
        self.fix_issue_16_primary_keys = fix_issue_16_primary_keys
        self.include_table_name_in_column = include_table_name_in_column
        self.compute_sc_link = compute_sc_link
        self.compute_cv_link = compute_cv_link

        self.counted_db_ids = set()
        self.preprocessed_schemas = {}

        self.tokenizer = BertTokenizer.from_pretrained(bert_version)
        self.stanfordcore_nlp = StanfordCoreNLP(os.path.abspath(
            os.path.join(os.path.dirname(__file__), '../../../third_party/stanford-corenlp-full-2018-10-05')))

        column_types = ["text", "number", "time", "boolean", "others"]
        self.tokenizer.add_tokens([f"<type: {t}>" for t in column_types])

    def _tokenize(self, presplit, unsplit):
        return self.tokenizer.tokenize(unsplit) if self.tokenizer else presplit

    def add_item(self, item, section, validation_info):
        preprocessed = self.preprocess_item(item, validation_info)
        self.texts[section].append(preprocessed)

    def preprocess_item(self, item, validation_info):
        question = self._tokenize(item.text, item.orig['question'])
        preproc_schema = self._preprocess_schema(item.schema)
        if self.compute_sc_link:
            question_bert_tokens = Bertokens(question)
            sc_link = question_bert_tokens.bert_schema_linking(
                preproc_schema.normalized_column_names,
                preproc_schema.normalized_table_names
            )
        else:
            sc_link = {"q_col_match": {}, "q_tab_match": {}}

        if self.compute_cv_link:
            question_bert_tokens = Bertokens(question)
            cv_link = compute_cell_value_linking(question_bert_tokens.normalized_pieces, item.schema)
        else:
            cv_link = {"num_date_match": {}, "cell_match": {}}

        # dependency parsing
        dependency_tree = self.stanfordcore_nlp.dependency_parse(item.orig['question'])
        word_adj_tuple = []
        start_index = 0
        for index, tuple in enumerate(dependency_tree):
            if tuple[0] == 'ROOT':
                start_index = index
                continue
            word_adj_tuple.append((tuple[1] + start_index - 1, tuple[2] + start_index - 1))

        return {
            'raw_question': item.orig['question'],
            'question': question,
            'db_id': item.schema.db_id,
            'sc_link': sc_link,
            'cv_link': cv_link,
            'columns': preproc_schema.column_names,
            'tables': preproc_schema.table_names,
            'table_bounds': preproc_schema.table_bounds,
            'column_to_table': preproc_schema.column_to_table,
            'table_to_columns': preproc_schema.table_to_columns,
            'foreign_keys': preproc_schema.foreign_keys,
            'foreign_keys_tables': preproc_schema.foreign_keys_tables,
            'primary_keys': preproc_schema.primary_keys,
            'word_adj_tuple': word_adj_tuple
        }

    def validate_item(self, item, section):
        question = self._tokenize(item.text, item.orig['question'])
        preproc_schema = self._preprocess_schema(item.schema)

        num_words = len(question) + 2 + \
                        sum(len(c) + 1 for c in preproc_schema.column_names) + \
                        sum(len(t) + 1 for t in preproc_schema.table_names)
        return (False, None) if num_words > 512 else (True, None)

    def _preprocess_schema(self, schema):
        if schema.db_id in self.preprocessed_schemas:
            return self.preprocessed_schemas[schema.db_id]
        result = preprocess_schema_uncached(schema, self._tokenize,
                                            self.include_table_name_in_column,
                                            self.fix_issue_16_primary_keys, bert=True)
        self.preprocessed_schemas[schema.db_id] = result
        return result

    def save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        self.tokenizer.save_pretrained(self.data_dir)

        for section, texts in self.texts.items():
            with open(os.path.join(self.data_dir, f'{section}.jsonl'), 'w') as f:
                for text in texts:
                    f.write(json.dumps(text) + '\n')

    def load(self):
        self.tokenizer = BertTokenizer.from_pretrained(self.data_dir)

@registry.register('encoder', 'Encoder4Bert')
class Encoder4Bert(torch.nn.Module):
    Preproc = EncoderPreproc4Bert
    batched = True

    def __init__(
            self,
            device,
            preproc,
            update_config={},
            bert_token_type=False,
            bert_version="bert-base-uncased",
            summarize_header="first",
            use_column_type=True,
            include_in_memory=('question', 'column', 'table')):
        super().__init__()
        self._device = device
        self.preproc = preproc

        self.bert_token_type = bert_token_type
        self.base_enc_hidden_size = 1024 if bert_version == "bert-large-uncased-whole-word-masking" else 768

        assert summarize_header in ["first", "avg"]
        self.summarize_header = summarize_header
        self.enc_hidden_size = self.base_enc_hidden_size
        self.use_column_type = use_column_type

        self.include_in_memory = set(include_in_memory)

        self.encs_update = registry.instantiate(
            spider_enc_modules.StructureAwareGraphAggrUpdate,
            update_config,
            unused_keys={"name"},
            device=self._device,
            hidden_size=self.enc_hidden_size,
        )

        self.bert_model = BertModel.from_pretrained(bert_version)
        self.tokenizer = self.preproc.tokenizer
        self.bert_model.resize_token_embeddings(len(self.tokenizer))

    def forward(self, descs):
        batch_token_lists = []
        batch_id_to_retrieve_question = []
        batch_id_to_retrieve_column = []
        batch_id_to_retrieve_table = []
        if self.summarize_header == "avg":
            batch_id_to_retrieve_column_2 = []
            batch_id_to_retrieve_table_2 = []
        long_seq_set = set()
        batch_id_map = {}  # some long examples are not included
        for batch_idx, desc in enumerate(descs):
            qs = self.pad_single_sentence_for_bert(desc['question'], cls=True)
            if self.use_column_type:
                cols = [self.pad_single_sentence_for_bert(c, cls=False) for c in desc['columns']]
            else:
                cols = [self.pad_single_sentence_for_bert(c[:-1], cls=False) for c in desc['columns']]
            tabs = [self.pad_single_sentence_for_bert(t, cls=False) for t in desc['tables']]

            token_list = qs + [c for col in cols for c in col] + \
                             [t for tab in tabs for t in tab]
            assert self.check_bert_seq(token_list)
            if len(token_list) > 512:
                long_seq_set.add(batch_idx)
                continue

            q_b = len(qs)
            col_b = q_b + sum(len(c) for c in cols)
            # leave out [CLS] and [SEP]
            question_indexes = list(range(q_b))[1:-1]
            # use the first representation for column/table
            column_indexes = \
                    np.cumsum([q_b] + [len(token_list) for token_list in cols[:-1]]).tolist()
            table_indexes = \
                    np.cumsum([col_b] + [len(token_list) for token_list in tabs[:-1]]).tolist()
            if self.summarize_header == "avg":
                column_indexes_2 = \
                        np.cumsum([q_b - 2] + [len(token_list) for token_list in cols]).tolist()[1:]
                table_indexes_2 = \
                        np.cumsum([col_b - 2] + [len(token_list) for token_list in tabs]).tolist()[1:]

            indexed_token_list = self.tokenizer.convert_tokens_to_ids(token_list)
            batch_token_lists.append(indexed_token_list)

            question_rep_ids = torch.LongTensor(question_indexes).to(self._device)
            batch_id_to_retrieve_question.append(question_rep_ids)
            column_rep_ids = torch.LongTensor(column_indexes).to(self._device)
            batch_id_to_retrieve_column.append(column_rep_ids)
            table_rep_ids = torch.LongTensor(table_indexes).to(self._device)
            batch_id_to_retrieve_table.append(table_rep_ids)
            if self.summarize_header == "avg":
                assert (all(i2 >= i1 for i1, i2 in zip(column_indexes, column_indexes_2)))
                column_rep_ids_2 = torch.LongTensor(column_indexes_2).to(self._device)
                batch_id_to_retrieve_column_2.append(column_rep_ids_2)
                assert (all(i2 >= i1 for i1, i2 in zip(table_indexes, table_indexes_2)))
                table_rep_ids_2 = torch.LongTensor(table_indexes_2).to(self._device)
                batch_id_to_retrieve_table_2.append(table_rep_ids_2)

            batch_id_map[batch_idx] = len(batch_id_map)

        padded_token_lists, att_mask_lists, tok_type_lists = self.pad_sequence_for_bert_batch(batch_token_lists)
        tokens_tensor = torch.LongTensor(padded_token_lists).to(self._device)
        att_masks_tensor = torch.LongTensor(att_mask_lists).to(self._device)

        if self.bert_token_type:
            tok_type_tensor = torch.LongTensor(tok_type_lists).to(self._device)
            bert_output = self.bert_model(tokens_tensor,
                                          attention_mask=att_masks_tensor, token_type_ids=tok_type_tensor)[0]
        else:
            bert_output = self.bert_model(tokens_tensor,
                                          attention_mask=att_masks_tensor)[0]

        enc_output = bert_output
        column_pointer_maps = [
            {
                i: [i]
                for i in range(len(desc['columns']))
            }
            for desc in descs
        ]
        table_pointer_maps = [
            {
                i: [i]
                for i in range(len(desc['tables']))
            }
            for desc in descs
        ]

        assert not long_seq_set

        result = []
        for batch_idx, desc in enumerate(descs):
            c_boundary = list(range(len(desc["columns"]) + 1))
            t_boundary = list(range(len(desc["tables"]) + 1))

            if batch_idx in long_seq_set:
                q_enc, col_enc, tab_enc = self.encoder_long_seq(desc)
            else:
                bert_batch_idx = batch_id_map[batch_idx]
                q_enc = enc_output[bert_batch_idx][batch_id_to_retrieve_question[bert_batch_idx]]
                col_enc = enc_output[bert_batch_idx][batch_id_to_retrieve_column[bert_batch_idx]]
                tab_enc = enc_output[bert_batch_idx][batch_id_to_retrieve_table[bert_batch_idx]]

                if self.summarize_header == "avg":
                    col_enc_2 = enc_output[bert_batch_idx][batch_id_to_retrieve_column_2[bert_batch_idx]]
                    tab_enc_2 = enc_output[bert_batch_idx][batch_id_to_retrieve_table_2[bert_batch_idx]]

                    col_enc = (col_enc + col_enc_2) / 2.0  # avg of first and last token
                    tab_enc = (tab_enc + tab_enc_2) / 2.0  # avg of first and last token

            assert q_enc.size()[0] == len(desc["question"])
            assert col_enc.size()[0] == c_boundary[-1]
            assert tab_enc.size()[0] == t_boundary[-1]

            q_enc_new_item, c_enc_new_item, t_enc_new_item, align_mat_item = \
                    self.encs_update.forward_unbatched(
                    desc,
                    q_enc.unsqueeze(1),
                    col_enc.unsqueeze(1),
                    c_boundary,
                    tab_enc.unsqueeze(1),
                    t_boundary)

            memory = []
            if 'question' in self.include_in_memory:
                memory.append(q_enc_new_item)
            if 'column' in self.include_in_memory:
                memory.append(c_enc_new_item)
            if 'table' in self.include_in_memory:
                memory.append(t_enc_new_item)
            memory = torch.cat(memory, dim=1)

            result.append(SpiderEncoderState(
                state=None,
                memory=memory,
                question_memory=q_enc_new_item,
                schema_memory=torch.cat((c_enc_new_item, t_enc_new_item), dim=1),
                words=desc['question'],
                pointer_memories={
                    'column': c_enc_new_item,
                    'table': t_enc_new_item,
                },
                pointer_maps={
                    'column': column_pointer_maps[batch_idx],
                    'table': table_pointer_maps[batch_idx],
                },
                m2c_align_mat=align_mat_item[0],
                m2t_align_mat=align_mat_item[1],
            ))
        return result

    @DeprecationWarning
    def encoder_long_seq(self, desc):
        """
        Since bert cannot handle sequence longer than 512, each column/table is encoded individually
        The representation of a column/table is the vector of the first token [CLS]
        """
        qs = self.pad_single_sentence_for_bert(desc['question'], cls=True)
        cols = [self.pad_single_sentence_for_bert(c, cls=True) for c in desc['columns']]
        tabs = [self.pad_single_sentence_for_bert(t, cls=True) for t in desc['tables']]

        enc_q = self._bert_encode(qs)
        enc_col = self._bert_encode(cols)
        enc_tab = self._bert_encode(tabs)
        return enc_q, enc_col, enc_tab

    @DeprecationWarning
    def _bert_encode(self, toks):
        if not isinstance(toks[0], list):  # encode question words
            indexed_tokens = self.tokenizer.convert_tokens_to_ids(toks)
            tokens_tensor = torch.tensor([indexed_tokens]).to(self._device)
            outputs = self.bert_model(tokens_tensor)
            return outputs[0][0, 1:-1]  # remove [CLS] and [SEP]
        else:
            max_len = max(len(it) for it in toks)
            tok_ids = []
            for item_toks in toks:
                item_toks = item_toks + [self.tokenizer.pad_token] * (max_len - len(item_toks))
                indexed_tokens = self.tokenizer.convert_tokens_to_ids(item_toks)
                tok_ids.append(indexed_tokens)

            tokens_tensor = torch.tensor(tok_ids).to(self._device)
            outputs = self.bert_model(tokens_tensor)
            return outputs[0][:, 0, :]

    def check_bert_seq(self, toks):
        return (
            toks[0] == self.tokenizer.cls_token
            and toks[-1] == self.tokenizer.sep_token
        )

    def pad_single_sentence_for_bert(self, toks, cls=True):
        if cls:
            return [self.tokenizer.cls_token] + toks + [self.tokenizer.sep_token]
        else:
            return toks + [self.tokenizer.sep_token]

    def pad_sequence_for_bert_batch(self, tokens_lists):
        pad_id = self.tokenizer.pad_token_id
        max_len = max(len(it) for it in tokens_lists)
        assert max_len <= 512
        toks_ids = []
        att_masks = []
        tok_type_lists = []
        for item_toks in tokens_lists:
            padded_item_toks = item_toks + [pad_id] * (max_len - len(item_toks))
            toks_ids.append(padded_item_toks)

            _att_mask = [1] * len(item_toks) + [0] * (max_len - len(item_toks))
            att_masks.append(_att_mask)

            first_sep_id = padded_item_toks.index(self.tokenizer.sep_token_id)
            assert first_sep_id > 0
            _tok_type_list = [0] * (first_sep_id + 1) + [1] * (max_len - first_sep_id - 1)
            tok_type_lists.append(_tok_type_list)
        return toks_ids, att_masks, tok_type_lists

'''
GAP version
'''

class BartTokens:
    def __init__(self, text, tokenizer):
        self.text = text
        # pieces is tokenized tokens.
        self.tokenizer = tokenizer
        self.normalized_pieces = None
        self.idx_map = None
        self.normalize_toks()

    def normalize_toks(self):
        tokens = nltk.word_tokenize(self.text.replace("'", " ' ").replace('"', ' " '))
        self.idx_map = {}
        # This map piece index to token index
        toks = []
        for i, tok in enumerate(tokens):
            self.idx_map[i] = len(toks)
            toks.extend(self.tokenizer.tokenize(tok, add_prefix_space=True))

        normalized_toks = []
        for tok in tokens:
            ann = corenlp.annotate(tok, annotators=["tokenize", "ssplit", "lemma"])
            lemmas = [tok.lemma.lower() for sent in ann.sentence for tok in sent.token]
            lemma_word = " ".join(lemmas)
            normalized_toks.append(lemma_word)
        self.normalized_pieces = normalized_toks

    def bart_schema_linking(self, columns, tables):
        question_tokens = self.normalized_pieces
        column_tokens = [c.normalized_pieces for c in columns]
        table_tokens = [t.normalized_pieces for t in tables]
        sc_link = compute_schema_linking(question_tokens, column_tokens, table_tokens)

        new_sc_link = {}
        for m_type in sc_link:
            _match = {}
            for ij_str in sc_link[m_type]:
                q_id_str, col_tab_id_str = ij_str.split(",")
                q_id, col_tab_id = int(q_id_str), int(col_tab_id_str)
                real_q_id = self.idx_map[q_id]
                _match[f"{real_q_id},{col_tab_id}"] = sc_link[m_type][ij_str]
            new_sc_link[m_type] = _match
        return new_sc_link

    def bart_cv_linking(self, schema, db_path):
        question_tokens = self.normalized_pieces
        cv_link = compute_cell_value_linking_bart(question_tokens, schema, db_path)

        new_cv_link = {}
        for m_type in cv_link:
            if m_type != "normalized_token":
                _match = {}
                for ij_str in cv_link[m_type]:
                    q_id_str, col_tab_id_str = ij_str.split(",")
                    q_id, col_tab_id = int(q_id_str), int(col_tab_id_str)
                    real_q_id = self.idx_map[q_id]
                    _match[f"{real_q_id},{col_tab_id}"] = cv_link[m_type][ij_str]

                new_cv_link[m_type] = _match
            else:
                new_cv_link[m_type] = cv_link[m_type]
        return new_cv_link

def preprocess_schema_uncached_bart(schema,
                               tokenizer,
                               tokenize_func,
                               include_table_name_in_column,
                               fix_issue_16_primary_keys,
                               bart=False):
    """If it's bert, we also cache the normalized version of
    question/column/table for schema linking"""
    r = PreprocessedSchema()

    if bart: assert not include_table_name_in_column

    last_table_id = None
    for i, column in enumerate(schema.columns):
        col_toks = tokenize_func(
            column.name, column.unsplit_name)

        # assert column.type in ["text", "number", "time", "boolean", "others"]
        type_tok = f'<type: {column.type}>'
        if bart:
            # for bert, we take the representation of the first word
            column_name = col_toks + [type_tok]
            r.normalized_column_names.append(BartTokens(column.unsplit_name, tokenizer))
        else:
            column_name = [type_tok] + col_toks

        if include_table_name_in_column:
            if column.table is None:
                table_name = ['<any-table>']
            else:
                table_name = tokenize_func(
                    column.table.name, column.table.unsplit_name)
            column_name += ['<table-sep>'] + table_name
        r.column_names.append(column_name)

        table_id = None if column.table is None else column.table.id
        r.column_to_table[str(i)] = table_id
        if table_id is not None:
            columns = r.table_to_columns.setdefault(str(table_id), [])
            columns.append(i)
        if last_table_id != table_id:
            r.table_bounds.append(i)
            last_table_id = table_id

        if column.foreign_key_for is not None:
            r.foreign_keys[str(column.id)] = column.foreign_key_for.id
            r.foreign_keys_tables[str(column.table.id)].add(column.foreign_key_for.table.id)

    r.table_bounds.append(len(schema.columns))
    assert len(r.table_bounds) == len(schema.tables) + 1

    for table in schema.tables:
        table_toks = tokenize_func(
            table.name, table.unsplit_name)
        r.table_names.append(table_toks)
        if bart:
            r.normalized_table_names.append(BartTokens(table.unsplit_name, tokenizer))
    last_table = schema.tables[-1]

    r.foreign_keys_tables = serialization.to_dict_with_sorted_values(r.foreign_keys_tables)
    r.primary_keys = [
        column.id
        for table in schema.tables
        for column in table.primary_keys
    ] if fix_issue_16_primary_keys else [
        column.id
        for column in last_table.primary_keys
        for table in schema.tables
    ]

    return r


class EncoderPreproc4Gap(EncoderPreproc):
    def __init__(
            self,
            save_path,
            db_path,
            fix_issue_16_primary_keys=False,
            include_table_name_in_column=False,
            plm_version="facebook/bart-large",
            compute_sc_link=True,
            compute_cv_link=False):
        self.data_dir = os.path.join(save_path, 'enc')
        self.db_path = db_path
        self.texts = collections.defaultdict(list)
        self.fix_issue_16_primary_keys = fix_issue_16_primary_keys
        self.include_table_name_in_column = include_table_name_in_column
        self.compute_sc_link = compute_sc_link
        self.compute_cv_link = compute_cv_link

        self.counted_db_ids = set()
        self.preprocessed_schemas = {}

        self.tokenizer = BartTokenizer.from_pretrained(plm_version)

        column_types = ["text", "number", "time", "boolean", "others"]
        self.tokenizer.add_tokens([f"<type: {t}>" for t in column_types])

        self.stanfordcore_nlp = StanfordCoreNLP(os.path.abspath(
                os.path.join(os.path.dirname(__file__), '../../../third_party/stanford-corenlp-full-2018-10-05')))

    def _tokenize(self, presplit, unsplit):
        # I want to keep this tokenization consistent with BartTokens.
        # Presplit is required here.
        tokens = nltk.word_tokenize(unsplit.replace("'", " ' ").replace('"', ' " '))
        toks = []
        for token in tokens:
            toks.extend(self.tokenizer.tokenize(token, add_prefix_space=True))
        return toks

    def add_item(self, item, section, validation_info):
        preprocessed = self.preprocess_item(item, validation_info)
        self.texts[section].append(preprocessed)

    def preprocess_item(self, item, validation_info):
        # For bart, there is a punctuation issue if we want to merge it back to words.
        # So here I will use nltk to further tokenize the sentence first.
        question = self._tokenize(item.text, item.orig['question'])
        preproc_schema = self._preprocess_schema(item.schema)
        question_bart_tokens = BartTokens(item.orig['question'], self.tokenizer)
        if self.compute_sc_link:
            # We do not want to transform pieces back to word.
            sc_link = question_bart_tokens.bart_schema_linking(
                preproc_schema.normalized_column_names,
                preproc_schema.normalized_table_names
            )
        else:
            sc_link = {"q_col_match": {}, "q_tab_match": {}}

        if self.compute_cv_link:
            cv_link = question_bart_tokens.bart_cv_linking(
                item.schema, self.db_path)
        else:
            cv_link = {"num_date_match": {}, "cell_match": {}}

        # dependency parsing
        dependency_tree = self.stanfordcore_nlp.dependency_parse(item.orig['question'])
        word_adj_tuple = []
        start_index = 0
        for index, tuple in enumerate(dependency_tree):
            if tuple[0] == 'ROOT':
                start_index = index
                continue
            word_adj_tuple.append((tuple[1] + start_index - 1, tuple[2] + start_index - 1))

        return {
            'raw_question': item.orig['question'],
            'question': question,
            'db_id': item.schema.db_id,
            'sc_link': sc_link,
            'cv_link': cv_link,
            'columns': preproc_schema.column_names,
            'tables': preproc_schema.table_names,
            'table_bounds': preproc_schema.table_bounds,
            'column_to_table': preproc_schema.column_to_table,
            'table_to_columns': preproc_schema.table_to_columns,
            'foreign_keys': preproc_schema.foreign_keys,
            'foreign_keys_tables': preproc_schema.foreign_keys_tables,
            'primary_keys': preproc_schema.primary_keys,
            'word_adj_tuple': word_adj_tuple,
        }

    def validate_item(self, item, section):
        question = self._tokenize(item.text, item.orig['question'])
        preproc_schema = self._preprocess_schema(item.schema)
        # 2 is for cls and sep special tokens. +1 is for sep
        num_words = len(question) + 2 + \
                        sum(len(c) + 1 for c in preproc_schema.column_names) + \
                        sum(len(t) + 1 for t in preproc_schema.table_names)
        return (False, None) if num_words > 512 else (True, None)

    def _preprocess_schema(self, schema):
        if schema.db_id in self.preprocessed_schemas:
            return self.preprocessed_schemas[schema.db_id]
        result = preprocess_schema_uncached_bart(schema, self.tokenizer, self._tokenize,
                                            self.include_table_name_in_column,
                                            self.fix_issue_16_primary_keys, bart=True)
        self.preprocessed_schemas[schema.db_id] = result
        return result

    def save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        self.tokenizer.save_pretrained(self.data_dir)

        for section, texts in self.texts.items():
            with open(os.path.join(self.data_dir, f'{section}.jsonl'), 'w') as f:
                for text in texts:
                    f.write(json.dumps(text) + '\n')

    def load(self):
        self.tokenizer = BartTokenizer.from_pretrained(self.data_dir)

@registry.register('encoder', 'Encoder4Gap')
class Encoder4Gap(torch.nn.Module):
    Preproc = EncoderPreproc4Gap
    batched = True

    def __init__(
          self,
          device,
          preproc,
          update_config={},
          plm_version="facebook/bart-large",
          summarize_header="first",
          use_column_type=True,
          include_in_memory=('question', 'column', 'table')):
        super().__init__()
        self._device = device
        self.preproc = preproc
        self.base_enc_hidden_size = 1024

        assert summarize_header in ["first", "avg"]
        self.summarize_header = summarize_header
        self.enc_hidden_size = self.base_enc_hidden_size
        self.use_column_type = use_column_type

        self.include_in_memory = set(include_in_memory)

        self.encs_update = registry.instantiate(
            spider_enc_modules.StructureAwareGraphAggrUpdate,
            update_config,
            unused_keys={"name"},
            device=self._device,
            hidden_size=self.enc_hidden_size
        )

        self.bert_model = BartModel.from_pretrained(plm_version)

        def replace_model_with_pretrained(model, path, prefix):
            restore_state_dict = torch.load(
                path, map_location=lambda storage, location: storage)
            keep_keys = []
            for key in restore_state_dict.keys():
                if key.startswith(prefix):
                    keep_keys.append(key)
            loaded_dict = {k.replace(prefix, ""): restore_state_dict[k] for k in keep_keys}
            model.load_state_dict(loaded_dict)

        self.tokenizer = self.preproc.tokenizer
        self.bert_model.resize_token_embeddings(50266)

        replace_model_with_pretrained(self.bert_model.encoder, os.path.join(
            "plm", "gap", "pretrained-checkpoint"), "bert.model.encoder.")
        self.bert_model.resize_token_embeddings(len(self.tokenizer))
        self.bert_model = self.bert_model.encoder
        self.bert_model.decoder = None

    def forward(self, descs):
        batch_token_lists = []
        batch_id_to_retrieve_question = []
        batch_id_to_retrieve_column = []
        batch_id_to_retrieve_table = []
        if self.summarize_header == "avg":
            batch_id_to_retrieve_column_2 = []
            batch_id_to_retrieve_table_2 = []
        long_seq_set = set()
        batch_id_map = {}
        for batch_idx, desc in enumerate(descs):
            qs = self.pad_single_sentence_for_bert(desc['question'], cls=True)
            if self.use_column_type:
                cols = [self.pad_single_sentence_for_bert(c, cls=False) for c in desc['columns']]
            else:
                cols = [self.pad_single_sentence_for_bert(c[:-1], cls=False) for c in desc['columns']]
            tabs = [self.pad_single_sentence_for_bert(t, cls=False) for t in desc['tables']]

            token_list = qs + [c for col in cols for c in col] + \
                         [t for tab in tabs for t in tab]
            assert self.check_bert_seq(token_list)
            if len(token_list) > 512:
                long_seq_set.add(batch_idx)
                continue

            q_b = len(qs)
            col_b = q_b + sum(len(c) for c in cols)
            # leave out [CLS] and [SEP]
            question_indexes = list(range(q_b))[1:-1]
            # use the first representation for column/table
            column_indexes = \
                np.cumsum([q_b] + [len(token_list) for token_list in cols[:-1]]).tolist()
            table_indexes = \
                np.cumsum([col_b] + [len(token_list) for token_list in tabs[:-1]]).tolist()
            if self.summarize_header == "avg":
                column_indexes_2 = \
                    np.cumsum([q_b - 2] + [len(token_list) for token_list in cols]).tolist()[1:]
                table_indexes_2 = \
                    np.cumsum([col_b - 2] + [len(token_list) for token_list in tabs]).tolist()[1:]

            indexed_token_list = self.tokenizer.convert_tokens_to_ids(token_list)
            batch_token_lists.append(indexed_token_list)

            question_rep_ids = torch.LongTensor(question_indexes).to(self._device)
            batch_id_to_retrieve_question.append(question_rep_ids)
            column_rep_ids = torch.LongTensor(column_indexes).to(self._device)
            batch_id_to_retrieve_column.append(column_rep_ids)
            table_rep_ids = torch.LongTensor(table_indexes).to(self._device)
            batch_id_to_retrieve_table.append(table_rep_ids)
            if self.summarize_header == "avg":
                assert (all(i2 >= i1 for i1, i2 in zip(column_indexes, column_indexes_2)))
                column_rep_ids_2 = torch.LongTensor(column_indexes_2).to(self._device)
                batch_id_to_retrieve_column_2.append(column_rep_ids_2)
                assert (all(i2 >= i1 for i1, i2 in zip(table_indexes, table_indexes_2)))
                table_rep_ids_2 = torch.LongTensor(table_indexes_2).to(self._device)
                batch_id_to_retrieve_table_2.append(table_rep_ids_2)

            batch_id_map[batch_idx] = len(batch_id_map)

        padded_token_lists, att_mask_lists, tok_type_lists = self.pad_sequence_for_bert_batch(batch_token_lists)
        tokens_tensor = torch.LongTensor(padded_token_lists).to(self._device)
        att_masks_tensor = torch.LongTensor(att_mask_lists).to(self._device)

        bert_output = self.bert_model(tokens_tensor, attention_mask=att_masks_tensor)[0]
        enc_output = bert_output

        column_pointer_maps = [
            {
                i: [i]
                for i in range(len(desc['columns']))
            }
            for desc in descs
        ]
        table_pointer_maps = [
            {
                i: [i]
                for i in range(len(desc['tables']))
            }
            for desc in descs
        ]

        assert len(long_seq_set) == 0  # remove them for now

        result = []
        for batch_idx, desc in enumerate(descs):
            c_boundary = list(range(len(desc["columns"]) + 1))
            t_boundary = list(range(len(desc["tables"]) + 1))

            if batch_idx in long_seq_set:
                q_enc, col_enc, tab_enc = self.encoder_long_seq(desc)
            else:
                bert_batch_idx = batch_id_map[batch_idx]
                q_enc = enc_output[bert_batch_idx][batch_id_to_retrieve_question[bert_batch_idx]]
                col_enc = enc_output[bert_batch_idx][batch_id_to_retrieve_column[bert_batch_idx]]
                tab_enc = enc_output[bert_batch_idx][batch_id_to_retrieve_table[bert_batch_idx]]

                if self.summarize_header == "avg":
                    col_enc_2 = enc_output[bert_batch_idx][batch_id_to_retrieve_column_2[bert_batch_idx]]
                    tab_enc_2 = enc_output[bert_batch_idx][batch_id_to_retrieve_table_2[bert_batch_idx]]

                    col_enc = (col_enc + col_enc_2) / 2.0  # avg of first and last token
                    tab_enc = (tab_enc + tab_enc_2) / 2.0  # avg of first and last token

            assert q_enc.size()[0] == len(desc["question"])
            assert col_enc.size()[0] == c_boundary[-1]
            assert tab_enc.size()[0] == t_boundary[-1]

            q_enc_new_item, c_enc_new_item, t_enc_new_item, align_mat_item = \
                self.encs_update.forward_unbatched(
                    desc,
                    q_enc.unsqueeze(1),
                    col_enc.unsqueeze(1),
                    c_boundary,
                    tab_enc.unsqueeze(1),
                    t_boundary)

            memory = []
            if 'question' in self.include_in_memory:
                memory.append(q_enc_new_item)
            if 'column' in self.include_in_memory:
                memory.append(c_enc_new_item)
            if 'table' in self.include_in_memory:
                memory.append(t_enc_new_item)
            memory = torch.cat(memory, dim=1)

            result.append(SpiderEncoderState(
                state=None,
                memory=memory,
                question_memory=q_enc_new_item,
                schema_memory=torch.cat((c_enc_new_item, t_enc_new_item), dim=1),
                words=desc['question'],
                pointer_memories={
                    'column': c_enc_new_item,
                    'table': t_enc_new_item,
                },
                pointer_maps={
                    'column': column_pointer_maps[batch_idx],
                    'table': table_pointer_maps[batch_idx],
                },
                m2c_align_mat=align_mat_item[0],
                m2t_align_mat=align_mat_item[1],
            ))
        return result

    @DeprecationWarning
    def encoder_long_seq(self, desc):
        """
        Since bert cannot handle sequence longer than 512, each column/table is encoded individually
        The representation of a column/table is the vector of the first token [CLS]
        """
        qs = self.pad_single_sentence_for_bert(desc['question'], cls=True)
        cols = [self.pad_single_sentence_for_bert(c, cls=True) for c in desc['columns']]
        tabs = [self.pad_single_sentence_for_bert(t, cls=True) for t in desc['tables']]

        enc_q = self._bert_encode(qs)
        enc_col = self._bert_encode(cols)
        enc_tab = self._bert_encode(tabs)
        return enc_q, enc_col, enc_tab

    @DeprecationWarning
    def _bert_encode(self, toks):
        if not isinstance(toks[0], list):  # encode question words
            indexed_tokens = self.tokenizer.convert_tokens_to_ids(toks)
            tokens_tensor = torch.tensor([indexed_tokens]).to(self._device)
            outputs = self.bert_model(tokens_tensor)
            return outputs[0][0, 1:-1]  # remove [CLS] and [SEP]
        else:
            max_len = max([len(it) for it in toks])
            tok_ids = []
            for item_toks in toks:
                item_toks = item_toks + [self.tokenizer.pad_token] * (max_len - len(item_toks))
                indexed_tokens = self.tokenizer.convert_tokens_to_ids(item_toks)
                tok_ids.append(indexed_tokens)

            tokens_tensor = torch.tensor(tok_ids).to(self._device)
            outputs = self.bert_model(tokens_tensor)
            return outputs[0][:, 0, :]

    def check_bert_seq(self, toks):
        if toks[0] == self.tokenizer.cls_token and toks[-1] == self.tokenizer.sep_token:
            return True
        else:
            return False

    def pad_single_sentence_for_bert(self, toks, cls=True):
        if cls:
            return [self.tokenizer.cls_token] + toks + [self.tokenizer.sep_token]
        else:
            return toks + [self.tokenizer.sep_token]

    def pad_sequence_for_bert_batch(self, tokens_lists):
        pad_id = self.tokenizer.pad_token_id
        max_len = max([len(it) for it in tokens_lists])
        assert max_len <= 512
        toks_ids = []
        att_masks = []
        tok_type_lists = []
        for item_toks in tokens_lists:
            padded_item_toks = item_toks + [pad_id] * (max_len - len(item_toks))
            toks_ids.append(padded_item_toks)

            _att_mask = [1] * len(item_toks) + [0] * (max_len - len(item_toks))
            att_masks.append(_att_mask)

            first_sep_id = padded_item_toks.index(self.tokenizer.sep_token_id)
            assert first_sep_id > 0
            _tok_type_list = [0] * (first_sep_id + 1) + [1] * (max_len - first_sep_id - 1)
            tok_type_lists.append(_tok_type_list)
        return toks_ids, att_masks, tok_type_lists