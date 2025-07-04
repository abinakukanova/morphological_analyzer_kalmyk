from collections import namedtuple
from operator import attrgetter

import torch
import torch.nn as nn
from tqdm import tqdm

from data_utils import inverse_transformation


class EncoderRNN(nn.Module):
    def __init__(self, embedding_size, hidden_size1, hidden_size2, vocab_len,
                 dropout_ratio=0.2, device=torch.device('cuda')):
        super(EncoderRNN, self).__init__()
        self.hidden_size1 = hidden_size1
        self.hidden_size2 = hidden_size2
        self.embedding_size = embedding_size
        
        self.embedding = nn.Embedding(vocab_len+1, embedding_size)
        self.char_gru = nn.GRU(embedding_size, hidden_size1, bidirectional=False, num_layers=1, batch_first=True)
        # self.word_gru = nn.GRU(hidden_size1, hidden_size2, bidirectional=True, num_layers=1, batch_first=True)  # УБРАТЬ
        self.dropout = nn.Dropout(dropout_ratio)
        self.device = device

        # XLM-RoBERTa
        from transformers import AutoTokenizer, AutoModel
        self.xlmr_tokenizer = AutoTokenizer.from_pretrained("xlm-roberta-base")
        self.xlmr_model = AutoModel.from_pretrained("xlm-roberta-base").to(self.device)
        self.xlmr_hidden_size = 768  # для base версии

        # Если нужно привести размерность к hidden_size2:
        self.context_proj = nn.Linear(self.xlmr_hidden_size, hidden_size2 * 2).to(self.device)  # если раньше было bidirectional


    def init_context_hidden(self):
        """Initializes the hidden units of each context gru

        """
        return torch.zeros(2, 1, self.hidden_size2).to(self.device)

    def init_char_hidden(self, batch_size):
        """Initializes the hidden units of each char gru

        """
        return torch.zeros(1, batch_size, self.hidden_size1).to(self.device)


    def forward(self, x, sentence_text):
        # x — тензор индексов символов, sentence_text — строка (или список токенов)
        self.char_gru_hidden = self.init_char_hidden(x.size(1))
        # Embedding layer
        char_embeddings = self.embedding(x)
        char_embeddings = self.dropout(char_embeddings)
        # First-level gru layer (char-gru to generate word embeddings)
        _, word_embeddings = self.char_gru(char_embeddings.view(char_embeddings.shape[1:]), self.char_gru_hidden)
        word_embeddings = self.dropout(word_embeddings)

        # XLM-RoBERTa context embeddings
        with torch.no_grad():
            inputs = self.xlmr_tokenizer(sentence_text, return_tensors="pt", return_offsets_mapping=True)
            for k in ['input_ids', 'attention_mask']:
                if k in inputs:
                    inputs[k] = inputs[k].to(self.device)
            
            outputs = self.xlmr_model(**{k: v for k, v in inputs.items() if k != "offset_mapping"})
            last_hidden = outputs.last_hidden_state.squeeze(0) 
            word_ids = inputs.word_ids(batch_index=0)
            xlmr_embeddings = []
            for word_idx in range(len(sentence_text)):
            # Индексы subword-токенов, относящихся к этому слову
                subword_indices = [i for i, w_id in enumerate(word_ids) if w_id == word_idx]
                if subword_indices:
        # Усредняем эмбеддинги subword-токенов
                    emb = last_hidden[subword_indices].mean(dim=0)
                    xlmr_embeddings.append(emb)
            xlmr_embeddings = torch.stack(xlmr_embeddings)
        context_embeddings = self.context_proj(xlmr_embeddings)
        context_embeddings = self.dropout(context_embeddings)
        return word_embeddings[0], context_embeddings


class DecoderRNN(nn.Module):
    """ The module generates characters and tags sequentially to construct a morphological analysis

    Inputs a context representation of a word and apply grus
    to predict the characters in the root form and the tags in the analysis respectively

    """

    def __init__(self, embedding_size, hidden_size, vocab, dropout_ratio=0):
        """Initialize the decoder object

        Args:
            embedding_size (int): The dimension of embeddings
                (output embeddings includes character for roots and tags for analyzes)
            hidden_size (int): The number of units in gru
            vocab (dict): Vocab dictionary where keys are either characters or tags and the values are integer
            dropout_ratio(float): Dropout ratio, dropout applied to the outputs of both gru and embedding modules
        """
        super(DecoderRNN, self).__init__()

        # Hyper parameters
        self.hidden_size = hidden_size

        # Vocab and inverse vocab to converts output indexes to characters and tags
        self.vocab = vocab
        self.index2token = {v: k for k, v in vocab.items()}
        self.vocab_size = len(vocab)

        # Layers
        self.W = nn.Linear(2 * hidden_size, hidden_size)
        self.embedding = nn.Embedding(len(vocab)+1, embedding_size)
        self.gru = nn.GRU(embedding_size, hidden_size, 2, batch_first=True)
        self.classifier = nn.Linear(hidden_size, len(vocab))
        self.dropout = nn.Dropout(p=dropout_ratio)
        self.relu = nn.ReLU()
        self.softmax = nn.Softmax(dim=1)

    def forward(self, word_embeddings, context_vectors, y):
        """Forward pass of DecoderRNN

        Inputs a context-aware vector of a word and produces an analysis consists of root+tags

        Args:
            word_embedding (`torch.tensor`): word representations (outputs of char GRU)
            context_vector (`torch.tensor`): Context-aware representations of a words
            y (tuple): target tensors (encoded lemmas or encoded morph tags)

        Returns:
            `torch.tensor`: scores in each time step
        """

        # Initilize gru hidden units with context vector (encoder output)
        context_vectors = self.relu(self.W(context_vectors))
        hidden = torch.cat([context_vectors.view(1, *context_vectors.size()),
                            word_embeddings.view(1, *context_vectors.size())], 0)

        embeddings = self.embedding(y)
        embeddings = self.dropout(embeddings)
        outputs, _ = self.gru(embeddings, hidden)
        outputs = self.dropout(outputs)
        outputs = self.classifier(outputs)

        return outputs

    def predict(self, word_embedding, context_vector, max_len=50, device=torch.device('cuda')):
        """Forward pass of DecoderRNN for prediction only

        The loop for gru is stopped as soon as the end of sentence tag is produced twice.
        The first end of sentence tag indicates the end of the root while the second one indicates the end of tags

        Args:
            word_embedding (`torch.tensor`): word representation (outputs of char GRU
            context_vector (`torch.tensor`): Context-aware representation of a word
            max_len (int): Maximum length of produced analysis (Defaault: 50)
            device (`torch.device`): gpu or cpu

        Returns:
            tuple: (scores:`torch.tensor`, predictions:list)

        """

        # Initilize gru hidden units with context vector (encoder output)
        context_vector = context_vector.view(1, *context_vector.size())
        context_vector = self.relu(self.W(context_vector).view(1, 1, self.hidden_size))
        word_embedding = word_embedding.view(1, 1, self.hidden_size)
        hidden = torch.cat([context_vector, word_embedding], 0)

        # Oupput shape (maximum length of a an analyzer, output vocab size)
        scores = torch.zeros(max_len, self.vocab_size)

        # First predicted token is sentence start tag: 2
        predicted_token = torch.LongTensor(1).fill_(2).to(device)

        # Generate char or tag sequentially
        predictions = []
        for di in range(max_len):
            embedded = self.embedding(predicted_token).view(1, 1, -1)
            output, hidden = self.gru(embedded, hidden)
            output = self.classifier(output[0])
            scores[di] = output
            topv, topi = output.topk(1)
            predicted_token = topi.squeeze().detach().to(device)
            # Increase eos count if produced output is eos
            if predicted_token.item() == 1:
                break
            # Add predicted output to predictions if it is not a special character such as eos or padding
            if predicted_token.item() > 2:
                predictions.append(self.index2token[predicted_token.item()])

        return scores, predictions

    def predict_beam(self, word_embedding, context_vector, surface_len, beam_size=2, max_len=50, device=torch.device('cuda')):
        """Forward pass of DecoderRNN using beam search for prediction only

        The loop for gru is stopped as soon as the end of sentence tag is produced twice.
        The first end of sentence tag indicates the end of the root while the second one indicates the end of tags

        Args:
            word_embedding (`torch.tensor`): word representation (outputs of char GRU
            context_vector (`torch.tensor`): Context-aware representation of a word
            max_len (int): Maximum length of produced analysis (Defaault: 50)
            device (`torch.device`): gpu or cpu

        Returns:
            tuple: (scores:`torch.tensor`, predictions:list)

        """

        State = namedtuple('State', ['prediction', 'score', 'normalized_score', 'last_output', 'hidden'])

        # Initilize gru hidden units with context vector (encoder output)
        context_vector = context_vector.view(1, *context_vector.size())
        context_vector = self.relu(self.W(context_vector).view(1, 1, self.hidden_size))
        word_embedding = word_embedding.view(1, 1, self.hidden_size)
        hidden = torch.cat([context_vector, word_embedding], 0)

        states = [State('', 1.0, 1.0, torch.LongTensor(1).fill_(2).to(device), hidden)]
        completed_states = []

        while states:
            new_states = []
            while states:
                state = states.pop(0)
                if len(state.prediction) >= surface_len+2:
                    continue
                embedded = self.embedding(state.last_output).view(1, 1, -1)
                gru_outputs, _hidden = self.gru(embedded, state.hidden)
                scores = self.classifier(gru_outputs[0])
                scores = self.softmax(scores)
                scores, indices = scores.topk(beam_size)
                for ix, score in zip(indices[0], scores[0]):
                    predicted_token = ix.squeeze().detach().to(device)
                    _score = state.score * score

                    if predicted_token.item() == 1:
                        _prediction = state.prediction
                        prediction_len = len(_prediction) + 1.0
                        _normalized_score = (_score / ((5.0 + prediction_len) / 6.0)) * (surface_len / prediction_len)
                    else:
                        _prediction = state.prediction + self.index2token[predicted_token.item()]
                        _normalized_score = _score / ((5.0 + len(_prediction)) / 6.0)

                    new_state = State(_prediction, _score, _normalized_score, predicted_token, _hidden)

                    if predicted_token.item() == 1:
                        completed_states.append(new_state)
                    else:
                        new_states.append(new_state)

            states = sorted(new_states, key=attrgetter('normalized_score'), reverse=True)[:beam_size]
        return sorted(completed_states, key=attrgetter('normalized_score'), reverse=True)[0].prediction


class TransformerRNN(nn.Module):
    """ The module generates transformations from surface words to lemmas (as Insert, Delete, Replace labels)

    Inputs a context representation of a word and apply grus
    to predict the transformations between the surface and root forms

    """

    def __init__(self, embedding_size, hidden_size, vocab, input_vocab_size, dropout_ratio=0):
        """Initialize the decoder object

        Args:
            embedding_size (int): The dimension of embeddings
                (output embeddings includes character for roots and tags for analyzes)
            hidden_size (int): The number of units in gru
            vocab (dict): Vocab dictionary where keys are either characters or tags and the values are integer
            dropout_ratio(float): Dropout ratio, dropout applied to the outputs of both gru and embedding modules
        """
        super(TransformerRNN, self).__init__()

        # Hyper parameters
        self.hidden_size = hidden_size

        # Vocab and inverse vocab to converts output indexes to characters and tags
        self.vocab = vocab
        self.index2transformation = {v: k for k, v in vocab.items()}
        self.vocab_size = len(vocab)
        self.input_vocab_size = input_vocab_size

        # Layers
        self.W = nn.Linear(2 * hidden_size, hidden_size)
        self.embedding = nn.Embedding(self.input_vocab_size+1, embedding_size)
        self.gru = nn.GRU(embedding_size, hidden_size, 2, batch_first=True, bidirectional=True)
        self.classifier = nn.Linear(2 * hidden_size, len(vocab))
        self.dropout = nn.Dropout(p=dropout_ratio)
        self.relu = nn.ReLU()
        self.softmax = nn.Softmax(dim=2)

    def forward(self, word_embeddings, context_vectors, x):
        """Forward pass of DecoderRNN

        Inputs a context-aware vector of a word and produces an analysis consists of root+tags

        Args:
            word_embeddings (`torch.tensor`): word representations (outputs of char GRU)
            context_vectors (`torch.tensor`): Context-aware representations of a words
            x (`torch.tensor`): input tensors (character of words)

        Returns:
            `torch.tensor`: scores in each time step
        """

        # Initilize gru hidden units with context vector (encoder output)
        context_vectors = self.relu(self.W(context_vectors))
        hidden = torch.cat([context_vectors.view(1, *context_vectors.size()),
                            context_vectors.view(1, *context_vectors.size()),
                            word_embeddings.view(1, *context_vectors.size()),
                            word_embeddings.view(1, *context_vectors.size())], 0)

        embeddings = self.embedding(x.view(*x.shape[1:]))
        embeddings = self.dropout(embeddings)
        outputs, _ = self.gru(embeddings, hidden)
        outputs = self.dropout(outputs)
        outputs = self.classifier(outputs)

        return outputs

    def predict(self, word_embeddings, context_vectors, x, surfaces):
        """Forward pass of DecoderRNN for prediction only

        The loop for gru is stopped as soon as the end of sentence tag is produced twice.
        The first end of sentence tag indicates the end of the root while the second one indicates the end of tags

        Args:
            word_embeddings (`torch.tensor`): word representations (outputs of char GRU)
            context_vectors (`torch.tensor`): Context-aware representation of a word
            x (`torch.tensor`): input tensors (character of words)
            surfaces (list): List of surface words which will be transformed into lemma forms

        Returns:
            tuple: (scores:`torch.tensor`, predictions:list)

        """

        # Initilize gru hidden units with context vector (encoder output)
        context_vectors = self.relu(self.W(context_vectors))
        hidden = torch.cat([context_vectors.view(1, *context_vectors.size()),
                            context_vectors.view(1, *context_vectors.size()),
                            word_embeddings.view(1, *context_vectors.size()),
                            word_embeddings.view(1, *context_vectors.size())], 0)

        embeddings = self.embedding(x.view(*x.shape[1:]))
        embeddings = self.dropout(embeddings)
        outputs, _ = self.gru(embeddings, hidden)
        outputs = self.dropout(outputs)
        outputs = self.classifier(outputs)

        # Output shape (maximum length of a transformation, output size)
        scores = self.softmax(outputs).to('cpu')
        predictions = [[self.index2transformation[ix.item()] for ix in _scores] for _scores in torch.argmax(scores, 2)]
        predictions = [inverse_transformation(surface, prediction[:len(surface)])
                       for surface, prediction in zip(surfaces, predictions)]
        return scores, predictions


def test_encoder_decoder():
    train_data_path = '../data/2019/task2/UD_Afrikaans-AfriBooms/af_afribooms-um-train.conllu'
    from data_loaders import ConllDataset
    from torch.utils.data import DataLoader
    from predict import predict_sentence

    train_set = ConllDataset(train_data_path, max_sentences=1)
    train_loader = DataLoader(train_set)

    encoder = EncoderRNN(10, 50, 50, len(train_set.surface_char2id))
    decoder_lemma = DecoderRNN(10, 50, train_set.lemma_char2id)
    decoder_morph_tags = DecoderRNN(10, 50, train_set.morph_tag2id)

    # Define loss and optimizers
    criterion = nn.CrossEntropyLoss(ignore_index=0)

    # Create optimizers
    encoder_optimizer = torch.optim.Adam(encoder.parameters(), lr=0.001)
    decoder_lemma_optimizer = torch.optim.Adam(decoder_lemma.parameters(), lr=0.001)
    decoder_morph_tags_optimizer = torch.optim.Adam(decoder_morph_tags.parameters(), lr=0.001)

    # Let the training begin
    for _ in tqdm(range(1000)):
        # Training part
        encoder.train()
        decoder_lemma.train()
        decoder_morph_tags.train()
        for ix, (x, y1, y2) in enumerate(train_loader):

            # Clear gradients for each sentence
            encoder.zero_grad()
            decoder_lemma.zero_grad()
            decoder_morph_tags.zero_grad()

            # Run encoder
            word_embeddings, context_embeddings = encoder(x)

            # Run decoder for each word
            sentence_loss = 0.0
            for _y, decoder in zip([y1, y2], [decoder_lemma, decoder_morph_tags]):
                decoder_outputs = decoder(word_embeddings, context_embeddings, _y[0, :, :-1])

                for word_ix in range(word_embeddings.size(0)):
                    sentence_loss += criterion(decoder_outputs[word_ix], _y[0, word_ix, 1:])

                sentence_loss.backward(retain_graph=True)

                # Optimization
                encoder_optimizer.step()
                decoder_lemma_optimizer.step()
                decoder_morph_tags_optimizer.step()

    encoder.eval()
    decoder_lemma.eval()
    decoder_morph_tags.eval()
    # Make predictions and save to file
    for sentence in train_set.sentences:
        surface_words = [surface_word for surface_word in sentence.surface_words]
        conll_sentence = predict_sentence(surface_words, encoder, decoder_lemma, decoder_morph_tags, train_set)
        print(conll_sentence)

if __name__ == '__main__':
    test_encoder_decoder()