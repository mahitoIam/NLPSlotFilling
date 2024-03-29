"""
Adapted from this blog post:
https://charon.me/posts/pytorch/pytorch_seq2seq_5
"""
import math
import torch

class Encoder(torch.nn.Module):
    def __init__(self,
                 input_dim,
                 emb_dim,
                 hid_dim,
                 n_layers,
                 kernel_size,
                 dropout,
                 device,
                 max_length):
        super().__init__()
        
        assert kernel_size % 2 == 1, "Kernel size must be odd!"
        
        self.device = device
        
        self.scale = torch.FloatTensor([math.sqrt(0.5)]).to(device)
        
        self.tok_embedding = torch.nn.Embedding(input_dim, emb_dim).to(device)
        self.pos_embedding = torch.nn.Embedding(max_length, emb_dim).to(device)
        
        self.emb2hid = torch.nn.Linear(emb_dim, hid_dim).to(device)
        self.hid2emb = torch.nn.Linear(hid_dim, emb_dim).to(device)
        
        self.convs = torch.nn.ModuleList([
            torch.nn.Conv1d(in_channels = hid_dim, 
                            out_channels = 2 * hid_dim, 
                            kernel_size = kernel_size, 
                            padding = (kernel_size - 1) // 2).to(device)
        for _ in range(n_layers)])
        self.dropout = torch.nn.Dropout(dropout).to(device)
        
    def forward(self, src):
        batch_size = src.shape[0]
        src_len = src.shape[1]
        #create position tensor
        pos = torch.arange(0, src_len).repeat(batch_size, 1).to(self.device) #src_len

        #embed tokens and positions
        tok_embedded = self.tok_embedding(src) #[batch_size, src_len, emb_dim]
        pos_embedded = self.pos_embedding(pos) #[batch_size, src_len, emb_dim]
        embedded = self.dropout(tok_embedded + pos_embedded) #[batch_size, src_len, emb_dim]
        
        #pass embedded through linear layer to convert from emb dim to hid dim
        conv_input = self.emb2hid(embedded) #[batch_size, src_len, hid_dim]
        conv_input = conv_input.permute(0, 2, 1) #[batch_size, emb_dim, src_len]
        
        #begin convolutional blocks...
        for conv in self.convs:
            conved = conv(self.dropout(conv_input)) #[batch_size, 2*hid_dim, src_len]
            conved = torch.nn.functional.glu(conved, dim = 1) #[batch_size, hid_dim, src_len]
            #apply residual connection
            conved = (conved + conv_input) * self.scale #[batch_size, hid_dim, src_len]
            #set conv_input to conved for next loop iteration
            conv_input = conved
        
        #permute and convert back to emb dim
        conved = self.hid2emb(conved.permute(0, 2, 1)) #[batch_size, src_len, hid_dim]
        #elementwise sum output (conved) and input (embedded) to be used for attention
        combined = (conved + embedded) * self.scale #[batch_size, src_len, emb_dim]
        return conved, combined


class Decoder(torch.nn.Module):
    def __init__(self, 
                 output_dim, 
                 emb_dim, 
                 hid_dim, 
                 n_layers, 
                 kernel_size, 
                 dropout, 
                 tgt_pad_idx, 
                 device,
                 max_length):
        super().__init__()
        
        self.kernel_size = kernel_size
        self.tgt_pad_idx = tgt_pad_idx
        self.device = device
        
        self.scale = torch.FloatTensor([math.sqrt(0.5)]).to(device)
        
        self.tok_embedding = torch.nn.Embedding(output_dim, emb_dim).to(device)
        self.pos_embedding = torch.nn.Embedding(max_length, emb_dim).to(device)
        
        self.emb2hid = torch.nn.Linear(emb_dim, hid_dim).to(device)
        self.hid2emb = torch.nn.Linear(hid_dim, emb_dim).to(device)
        
        self.attn_hid2emb = torch.nn.Linear(hid_dim, emb_dim).to(device)
        self.attn_emb2hid = torch.nn.Linear(emb_dim, hid_dim).to(device)
        
        self.fc_out = torch.nn.Linear(emb_dim, output_dim).to(device)
        
        self.convs = torch.nn.ModuleList([
            torch.nn.Conv1d(in_channels = hid_dim, 
                            out_channels = 2 * hid_dim, 
                            kernel_size = kernel_size).to(device)
        for _ in range(n_layers)])
        
        self.dropout = torch.nn.Dropout(dropout).to(device)
      

    def calculate_attention(self, embedded, conved, encoder_conved, encoder_combined):      
        #permute and convert back to emb dim
        conved_emb = self.attn_hid2emb(conved.permute(0, 2, 1))
        combined = (conved_emb + embedded) * self.scale
        energy = torch.matmul(combined, encoder_conved.permute(0, 2, 1))
        attention = torch.nn.functional.softmax(energy, dim=2)
        attended_encoding = torch.matmul(attention, encoder_combined)
        #convert from emb dim -> hid dim
        attended_encoding = self.attn_emb2hid(attended_encoding)
        #apply residual connection
        attended_combined = (conved + attended_encoding.permute(0, 2, 1)) * self.scale
        return attention, attended_combined
        
    def forward(self, tgt, encoder_conved, encoder_combined):
        batch_size = tgt.shape[0]
        tgt_len = tgt.shape[1]
        #create position tensor
        pos = torch.arange(0, tgt_len).repeat(batch_size, 1).to(self.device) #[batch_size, tgt_len]
        
        #embed tokens and positions
        tok_embedded = self.tok_embedding(tgt) #[batch_size, tgt_len, emb_dim]
        pos_embedded = self.pos_embedding(pos) #[batch_size, tgt_len, emb_dim]      
        embedded = self.dropout(tok_embedded + pos_embedded) #[batch_size, tgt_len, emb_dim]

        #pass embedded through linear layer to go through emb_dim to hid_dim
        conv_input = self.emb2hid(embedded) #[batch_size, tgt_len, hid_dim]
        conv_input = conv_input.permute(0, 2, 1) #[batch_size, emb_dim, tgt_len]


        hid_dim = conv_input.shape[1] #hid_dim
        for conv in self.convs:
            #apply dropout
            conv_input = self.dropout(conv_input) #[batch_size, emb_dim, tgt_len]
            #need to pad so decoder can't cheat: [batch_size, hid_dim, kernel_size-1]
            padding = torch.zeros(batch_size, 
                                  hid_dim, 
                                  self.kernel_size - 1).fill_(self.tgt_pad_idx).to(self.device)
            padded_conv_input = torch.cat((padding, conv_input), dim = 2) #[batch _size, hid_dim, tgt_len + kernel_size - 1]
            #pass through convolutional layer
            conved = conv(padded_conv_input) #[batch_size, 2*hid dim, tgt_len]
            #pass through GLU activation function
            conved = torch.nn.functional.glu(conved, dim = 1) #[batch_size, hid_dim, tgt_len]
            #calculate attention
            attention, conved = self.calculate_attention(embedded, 
                                                         conved, 
                                                         encoder_conved, 
                                                         encoder_combined)
            #apply residual connection
            conved = (conved + conv_input) * self.scale #[batch_size, hid_dim, tgt_len]
            #set conv_input to conved for next loop iteration
            conv_input = conved
            
        conved = self.hid2emb(conved.permute(0, 2, 1)) #[batch_size, tgt_len, embid_dim]
        output = self.fc_out(self.dropout(conved)) #[batch_size, tgt_len, output_dim]
        return output, attention


class Seq2Seq(torch.nn.Module):
    def __init__(self, encoder, decoder, device):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.device = device
    def forward(self, src, tgt):
        # [batch_size, src_len, emb_dim], [batch_size, src_len, emb_dim]
        src = src.to(self.device)
        tgt = tgt.to(self.device)
        encoder_conved, encoder_combined = self.encoder(src)
        # [batch_size, tgt_len-1, output_dim], [batch_size, tgt_len-1, src_len]
        output, attention = self.decoder(tgt, encoder_conved, encoder_combined)
        return output, attention
