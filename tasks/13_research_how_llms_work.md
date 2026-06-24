# How Large Language Models (LLMs) Work

> A comprehensive overview of the architecture, training process, inference, and key concepts behind modern Large Language Models.

---

## 1. What is an LLM?

A **Large Language Model (LLM)** is a deep learning model trained on vast amounts of text data to understand, process, and generate human-like language. LLMs typically have billions of parameters (6B+) and are built on the **Transformer architecture**. They exhibit **emergent abilities** — capabilities not present in smaller models, such as in-context learning, reasoning, and chain-of-thought problem solving.

**Key characteristics:**
- Parameter count: typically 6 billion to 500+ billion
- Training data: terabytes of text from books, web pages, articles, code, etc.
- Self-supervised learning: no human-labeled data required for pre-training
- General-purpose: can be adapted to a wide range of tasks

---

## 2. Core Architecture: The Transformer

### 2.1 Overview

The Transformer architecture (introduced in the 2017 paper *"Attention Is All You Need"*) is the foundation of all modern LLMs. Unlike earlier recurrent neural networks (RNNs) that processed text sequentially, Transformers process all tokens in parallel using a **self-attention mechanism**.

### 2.2 Key Components

| Component | Purpose |
|-----------|---------|
| **Tokenization** | Converts raw text into integer token IDs using a subword vocabulary |
| **Embedding Layer** | Maps each token ID to a dense vector (embedding) |
| **Positional Encoding** | Adds position information since the model processes tokens in parallel |
| **Self-Attention** | Computes relationships between all tokens in the sequence |
| **Multi-Head Attention** | Runs multiple attention mechanisms in parallel, capturing different types of relationships |
| **Feed-Forward Network (FFN)** | Applies non-linear transformations to enrich representations |
| **Layer Normalization** | Stabilizes training by normalizing activations |
| **Residual Connections** | Skip connections that help gradient flow in deep networks |
| **Output Layer (Softmax)** | Converts final logits into a probability distribution over the vocabulary |

### 2.3 Self-Attention Mechanism (The Core Innovation)

Self-attention allows each token to "attend to" every other token in the input sequence. It works by computing three vectors for each token:

1. **Query (Q)** — What is this token looking for?
2. **Key (K)** — What does this token offer?
3. **Value (V)** — What information does this token carry?

The attention score between token *i* and token *j* is computed as:

```
Attention(Q, K, V) = softmax(Q × K^T / √d_k) × V
```

Where `d_k` is the dimension of the key vectors (scaling factor to prevent large dot products).

**Why this matters:** Self-attention captures long-range dependencies effectively — a word at position 1 can directly influence a word at position 1000, something RNNs struggled with.

### 2.4 Encoder-Decoder vs. Decoder-Only

| Architecture | Example Models | Use Case |
|-------------|----------------|----------|
| **Encoder-Decoder** | T5, BART, BERT (encoder only) | Translation, summarization, understanding tasks |
| **Decoder-Only** | GPT-4, LLaMA, Claude, Gemini | Text generation, chat, autoregressive completion |

Most modern LLMs use **decoder-only** architectures with **causal (masked) self-attention**, where each token can only attend to previous tokens (not future ones). This enables autoregressive generation.

---

## 3. Tokenization

Before text enters the model, it must be converted into numbers. This is done by a **tokenizer**, which splits text into **subword units** (tokens).

### Common Tokenization Methods

| Method | How It Works | Used By |
|--------|-------------|---------|
| **Byte-Pair Encoding (BPE)** | Iteratively merges the most frequent pair of adjacent characters/subwords | GPT-2, GPT-3, GPT-4, LLaMA, Claude |
| **WordPiece** | Uses a probabilistic likelihood model to decide merges (maximizes data likelihood) | BERT, DistilBERT |
| **SentencePiece** | Treats input as raw Unicode bytes; language-agnostic (no pre-tokenization) | T5, LLaMA, XLNet |
| **Unigram** | Starts with a large vocabulary and prunes tokens that least affect likelihood | ALBERT, XLNet |

**Key facts:**
- Typical vocabulary size: 30,000–200,000 tokens
- GPT-4 uses cl100k_base with 100,277 tokens; GPT-4o uses o200k_base with 200,019 tokens
- Tokenization is **deterministic** at inference — same input always yields same token IDs
- The tokenizer is frozen after training and must match between training and inference

---

## 4. Training Process

LLM training happens in three main stages:

### Stage 1: Self-Supervised Pre-Training

**Goal:** Learn general language understanding and knowledge from massive unlabeled text.

**Objective:** **Next-Token Prediction (Causal Language Modeling)** — given a sequence of tokens, predict the next token.

```
Input:  "The capital of France is"
Target: "Paris"
```

The model is trained to minimize **cross-entropy loss**:
```
L = -Σ log P(next_token | previous_tokens)
```

**Scale of pre-training:**
- Data: terabytes of text (CommonCrawl, Wikipedia, books, arXiv, GitHub, etc.)
- Compute: thousands of GPUs/TPUs running for weeks or months
- Cost: $10M–$100M+ for frontier models

**Parallel Training Techniques:**
- **Data parallelism**: split the batch across devices
- **Model parallelism**: split the model layers across devices
- **Pipeline parallelism**: layer-wise pipelining of forward/backward passes
- **Mixed precision training (FP16/BF16)**: reduces memory and speeds computation
- **ZeRO optimization**: partitions optimizer states, gradients, and parameters across devices

### Stage 2: Supervised Fine-Tuning (SFT)

**Goal:** Teach the model to follow instructions and produce desired response formats.

**Data:** Curated pairs of (prompt, ideal_response) — often human-written.

**Process:** The pre-trained model is further trained on these instruction-following examples using standard supervised learning.

**Why needed:** A pre-trained model can complete text in many possible ways; SFT constrains it toward human-preferred responses.

### Stage 3: Reinforcement Learning from Human Feedback (RLHF) / Alignment

**Goal:** Align model behavior with human values — making outputs helpful, honest, and harmless.

**Process:**

1. **Collect preference data**: Humans rank multiple model outputs for a given prompt (preferred vs. not preferred)
2. **Train a Reward Model**: A separate model learns to score outputs according to human preferences
3. **Optimize the policy**: The LLM (policy) is fine-tuned using reinforcement learning to maximize the reward signal, typically via **Proximal Policy Optimization (PPO)**

**Alternative approaches:**
- **Direct Preference Optimization (DPO)**: Skips the reward model and directly optimizes from preference pairs
- **Constitutional AI / RLAIF**: Uses AI feedback instead of human feedback for scaling

---

## 5. Inference (Text Generation)

Once trained, an LLM generates text **autoregressively** — one token at a time, feeding each new token back as input.

### 5.1 Basic Generation Loop

```
Input: "What is the capital of France?"
Model computes probabilities for every token in vocabulary
Sample/pick next token: "Paris"
Append to input: "What is the capital of France? Paris"
Model computes probabilities again
Sample/pick next token: "."
Continue until <EOS> or max length
```

### 5.2 Decoding / Sampling Strategies

| Strategy | How It Works | When to Use |
|----------|-------------|-------------|
| **Greedy Decoding** | Always pick the token with highest probability | Factual tasks, deterministic output |
| **Beam Search** | Track top-k candidate sequences | Translation, summarization |
| **Temperature Sampling** | Scale logits before softmax: lower T = more deterministic, higher T = more creative | Creative writing, diverse outputs |
| **Top-K Sampling** | Only sample from the K most likely tokens | Balanced creativity |
| **Top-P (Nucleus) Sampling** | Sample from the smallest set of tokens whose cumulative probability exceeds P | Industry standard — adapts dynamically |
| **Min-P Sampling** | Filter tokens based on a minimum probability relative to the top token | Newer, handles flat distributions better |

### 5.3 Key Parameters

| Parameter | Effect | Typical Range |
|-----------|--------|---------------|
| **Temperature** | Controls randomness: lower = more focused, higher = more creative | 0.0–2.0 (0.7 default) |
| **Top-K** | Limits candidate pool to K most likely tokens | 10–100 |
| **Top-P** | Limits candidate pool to tokens covering P probability mass | 0.8–0.95 |
| **Max Tokens** | Maximum length of generated response | Varies by model (e.g., 4096, 8192, 128K+) |
| **Frequency Penalty** | Penalizes tokens that have already appeared | 0.0–2.0 |
| **Presence Penalty** | Penalizes tokens that have appeared at all | 0.0–2.0 |

---

## 6. Model Compression for Deployment

Production LLMs are often too large to deploy efficiently. Several techniques reduce their size and speed up inference:

| Technique | Description | Benefit |
|-----------|-------------|---------|
| **Quantization** | Reduce precision of weights (FP32 → INT8/INT4) | 2-4x speedup, much less memory |
| **Pruning** | Remove less important weights or attention heads | Smaller model, faster inference |
| **Knowledge Distillation** | Train a smaller "student" model to mimic the larger "teacher" | Compact model with good performance |
| **Low-Rank Approximation** | Decompose weight matrices into smaller factors | Reduced parameter count |
| **KV-Cache** | Cache key/value vectors from previous tokens during generation | Avoids recomputation, speeds up autoregressive generation |

---

## 7. Key Concepts & Terminology

| Term | Definition |
|------|------------|
| **Context Window** | The maximum number of tokens the model can process at once (e.g., 8K, 32K, 128K, 1M+) |
| **Embedding** | A dense vector representation of a token (typically 768–12,288 dimensions) |
| **Hidden State** | The internal representation at each layer of the transformer |
| **Logits** | Raw, unnormalized scores output by the final linear layer (before softmax) |
| **Attention Head** | One instance of the self-attention mechanism; multiple heads run in parallel |
| **Feed-Forward Network (FFN)** | The MLP layers within each transformer block |
| **LayerNorm** | Layer normalization that stabilizes training |
| **Residual Connection** | Skip connection that adds input to output (helps gradient flow) |
| **Positional Encoding** | Information added to embeddings to encode token position |
| **In-Context Learning (ICL)** | The ability to learn from examples provided in the prompt at inference time (no weight updates) |
| **Emergent Abilities** | Capabilities that appear only at scale (reasoning, instruction following, etc.) |
| **Hallucination** | When the model generates plausible-sounding but incorrect information |

---

## 8. Evolution of Key LLMs (Timeline)

| Year | Model | Parameters | Significance |
|------|-------|-----------|--------------|
| 2017 | Transformer (original) | — | Introduced the architecture |
| 2018 | BERT (Google) | 340M | Bidirectional understanding, set NLP benchmarks |
| 2018 | GPT-1 (OpenAI) | 117M | First generative pre-trained transformer |
| 2019 | GPT-2 (OpenAI) | 1.5B | Showed scaling improves generation quality |
| 2020 | GPT-3 (OpenAI) | 175B | Demonstrated emergent abilities, in-context learning |
| 2022 | ChatGPT (OpenAI) | — | RLHF-aligned model, sparked mainstream LLM adoption |
| 2023 | GPT-4 (OpenAI) | ~1.7T (rumored) | Multi-modal, strong reasoning |
| 2023 | LLaMA 2 (Meta) | 7B–70B | Open-source, competitive performance |
| 2024 | Claude 3 (Anthropic) | — | Advanced reasoning, safety-focused |
| 2024 | GPT-4o (OpenAI) | — | Native multimodal (text, vision, audio) |
| 2025+ | Reasoning models (o1, o3, R1) | — | Chain-of-thought reasoning at test time |

---

## 9. Limitations & Challenges

- **Hallucination**: Models confidently generate false information
- **Bias**: Training data biases can be reflected in outputs
- **Cost**: Training and inference are computationally expensive
- **Context window limits**: Despite progress, very long contexts remain challenging
- **Interpretability**: It's difficult to understand why a model produced a specific output
- **Safety**: Risk of misuse (misinformation, malicious content)
- **Environmental impact**: High energy consumption for training large models

---

## 10. Summary

LLMs are **Transformer-based neural networks** trained on massive text corpora via **self-supervised next-token prediction**. They undergo three training stages: **pre-training** (learning language patterns), **supervised fine-tuning** (learning to follow instructions), and **RLHF alignment** (learning human preferences). During inference, they generate text **autoregressively**, one token at a time, guided by **sampling strategies** like temperature, top-k, and top-p. The combination of scale, the Transformer architecture's parallel self-attention, and alignment techniques produces models capable of remarkable language understanding, generation, reasoning, and problem-solving.

---

*Research compiled: July 2025*
*Sources: "Attention Is All You Need" (Vaswani et al. 2017), "Understanding LLMs: From Training to Inference" (Liu et al. 2024), IBM, GeeksforGeeks, DataCamp, Huyenchip.com, and various LLM documentation.*
