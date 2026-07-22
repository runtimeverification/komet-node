---
name: writing-documentation
description: Write, edit, refine, or review documentation and explanatory prose — README files, docs/ pages, design docs, module/class doc comments, and architecture write-ups. Use this whenever the user asks to improve, clarify, simplify, tighten, proofread, or rewrite docs, or to remove jargon, empty phrases, vague wording, or sentence fragments. Apply it even when the request looks like a small wording tweak, because the same standards apply at any size.
---

# Writing and refining documentation

The goal of documentation is to transfer understanding to a reader as quickly and reliably as possible. Every sentence should either tell the reader something they can act on or remove a question they would otherwise have. Judge prose by how much it informs, not by how it sounds.

These standards apply both when you write new documentation and when you refine someone else's. When in doubt, prefer fewer words that say something concrete over more words that gesture at it.

## Match the abstraction level to the audience

Before you write or edit a passage, decide who reads it and at what level it should speak. Two questions settle this, and they shape every later choice about what to include.

- **Is the reader a user of the system or a contributor to it?** End-user documentation explains how to operate an interface: what to call, what to pass, and what comes back. Contributor documentation explains how the system works inside, so that someone can change it safely. The same fact often belongs in one and not the other, so identify the audience first and write to it consistently.
- **Does this passage describe an interface or an implementation detail?** An interface is the contract a reader can depend on. An implementation detail is how that contract happens to be met today. Keep the two clearly separated, and when it is not obvious which one you are describing, say so.

Once you know the level, hold it steady:

- In user-facing documentation, leave out implementation details. A reader who wants to call a method does not need to know how it is built, and the extra mechanism buries what they came for. The one exception is a detail that would otherwise create a wrong expectation — for example, an operation that looks atomic but is not, or a default that is about to change. Surface that detail, because hiding it misleads the reader.
- In contributor documentation, implementation details are essential, but they still need a budget. Include the detail that helps a reader understand or change the code, and leave out the rest. Too much mechanism drowns the main point as surely as too little starves it, so give each passage the amount of detail its context calls for and no more.

A mismatch in level is itself a defect, even when every sentence is individually correct. A paragraph that slides from interface to internals and back forces the reader to keep re-orienting, which is the opposite of what the documentation is for.

## Writing standards

1. **Write to inform, not to impress.** Do not reach for elevated vocabulary, clever framing, or an authoritative tone for its own sake. If a plain word works, use the plain word. Prose that tries to sound smart usually hides that it is saying little.

2. **Keep jargon to a minimum.** Jargon is any term whose meaning depends on insider knowledge. Some is unavoidable in technical writing, but every term you keep is a small toll the reader pays. Spend that toll only when the term earns it.

3. **Make sure every technical term is either well understood by the target audience or defined where it first appears.** Before using a term, ask who reads this document and whether they already share its meaning. A term like "shim" is a bad sign: it has no agreed definition, so it lets the writer feel precise while telling the reader almost nothing. Either name the concrete thing the term stands for ("the long-running process that holds the socket and persists state") or define it on first use.

4. **Write in full, grammatically complete sentences.** Avoid sentence fragments and half-finished thoughts. A fragment forces the reader to reconstruct the missing subject or verb, which is exactly the work good documentation should do for them. See [Repairing sentence fragments](#repairing-sentence-fragments) below for the common shapes and how to fix them.

5. **Prefer active voice, but do not force it.** "Python decodes the envelope" reads more directly than "the envelope is decoded by Python." Active voice names who does what, which removes ambiguity. Where the actor is genuinely unknown or irrelevant, passive voice is fine — do not contort a sentence to avoid it.

6. **Replace empty phrases with real information, or delete them.** When you find a phrase that carries little meaning, first try to replace it with the fact it was standing in for. If there is no such fact, remove the phrase entirely. See [Spotting empty phrases](#spotting-empty-phrases).

7. **State objective facts, not subjective claims.** Documentation describes what the system is and does; it does not argue that the system is good. The reader came to understand the tool, not to be persuaded of its quality, and praise from its own documentation carries no weight anyway. Avoid value judgments and promotional words such as "powerful", "blazing fast", "simple", "elegant", "robust", and "seamless". When you find one, replace it with the concrete, verifiable property behind it: instead of "a fast, lightweight server", write "a single-threaded HTTP server that runs each request in one interpreter process", and let the reader judge. If a characterization has no fact behind it, drop it.

## Spotting empty phrases

An empty phrase occupies space without changing what the reader knows. Common kinds:

- **Undefined jargon**, as in standard 3 above.
- **Contrasts that do not explain themselves.** "Rather than running a real validator, it ..." sets up a difference but never says what the difference is, so the reader learns nothing. Either explain the contrast concretely (for example, "real Stellar RPC is built around a mempool and ledger close, so ...") or drop it. A contrast is worth keeping only when the same sentence makes clear what is being contrasted and why it matters.
- **Filler preambles and intensifiers**: "It is worth noting that", "Of course", "Simply", "Essentially", "A guiding constraint:". These announce a thought instead of delivering it. Cut them and start with the thought.
- **Restatements** that repeat the previous clause in new words, and **vacuous qualifiers** ("now", "actually", "in practice") that hint at a comparison the reader cannot see.

Watch for words like "now survive restarts" or "those now live in K" in standalone documentation: "now" silently compares against an earlier design the reader does not know about.

## Repairing sentence fragments

Three shapes show up again and again in reference-style docs:

- **Label openers**: "The long-running process." or "The XDR boundary." followed by a separate sentence. Fold the label into a real sentence with a subject: "`StellarRpcServer` is the long-running process around the semantics."
- **Verb-first lines with no subject**: "Decodes the envelope and returns a pair." Give it the subject it describes: "`build_tx_request` decodes the envelope and returns a pair."
- **Noun-phrase or participle fragments**: "A helper that pretty-prints the config." or "Used by `demo.py` to render each step." Turn each into a full sentence, and prefer active voice for the participle form: "`demo.py` uses it to render each step."

An em-dash is a useful warning sign. An em-dash inside a complete sentence is fine, but an em-dash followed by a trailing fragment ("... and returns a pair — for the interpreter to splice in") often marks an underdeveloped thought that should become its own sentence or fold into the main clause. Treat a trailing em-dash as a prompt to check whether the thought after it is complete.

## What to leave alone

Restraint matters as much as correction. Do not turn every list into prose. These constructs are conventional and clear, and rewriting them as full sentences usually hurts readability:

- Tables and code blocks.
- Glossary or reference lists in `name — short description` form (an API method index, a flags table, a list of file roles).
- Rosters under a heading that already supplies the predicate, such as a "What's not yet implemented" list whose every item is implicitly "is not implemented."
- Numbered procedure steps written as imperatives ("Write the request file.", "Parse the state."), which are already complete sentences.

A self-explaining contrast, a passive sentence with an irrelevant actor, and a deliberately terse reference entry are all fine. Change something only when the change makes the reader's job easier.

## How to run a refinement pass

When asked to clean up documentation, the user usually gives one example of a problem. Treat that example as one instance of a whole class, and handle the class.

1. **Read the whole document set, not just the flagged spot.** The same problem almost always recurs across files. Read every doc in scope before editing.
2. **Find every instance of the class.** After reading, use `grep` to catch recurring patterns mechanically — for example, search for verb-first line starts, filler words, or contrast phrases — so you do not miss any.
3. **Judge each candidate.** Separate genuine problems from acceptable constructs (see [What to leave alone](#what-to-leave-alone)). Do not invent problems to fix.
4. **Fix each one in place**, keeping the original meaning. Replace empty phrases with the fact behind them, give fragments a subject, and prefer active voice.
5. **Verify the docs against reality when they describe behavior.** A confident but stale sentence is worse than a wordy one. If a doc claims the code does X, confirm the code still does X. (In this codebase, that check has already caught a doc describing a `chdir` the code no longer performs.)
6. **Verify your pass.** Re-run the `grep` scans to confirm no instances of the class remain, and reread the edited sections to make sure each still flows.
7. **Summarize for the user.** Give a short table of the meaningful changes (before → after, with a one-line reason), and state plainly what you deliberately left unchanged and why. The "left unchanged" part shows your judgment and lets the user catch anything you misjudged.

## Audit prose you write, not just prose you inherit

[The refinement pass above](#how-to-run-a-refinement-pass) is framed around cleaning up existing text, and that framing hides a trap: it is easy to scan inherited prose for these problems while treating a sentence you just wrote as finished. The standards apply equally to both. Before you consider a passage you authored done — whether a whole new document or a single replacement sentence — run the same checks against it (abstraction level, empty phrases, fragments, and the rest) exactly as you would on someone else's draft.

Two failures show up most often in freshly written prose, so check for them every time:

- **A first sentence that announces a thought instead of delivering it.** "What makes it different is the execution engine." promises a difference without naming it — a filler preamble disguised as a topic sentence. Cut it and start the paragraph with the concrete fact.
- **A clause that describes how the system is built, in user-facing prose.** Mechanism leaks in most easily here, because explaining the internals feels like supplying substance. Ask of each clause whether it tells the reader something they can act on or only how the machinery works, and move the latter to the contributor docs — unless it prevents a wrong expectation (see [Match the abstraction level](#match-the-abstraction-level-to-the-audience)).

Then read the passage once as its target reader and ask of each sentence: does this inform me, or does it only sound like it should? Delete or replace whatever fails.

## Scope discipline

Stay close to the class of change the user asked for. If you notice an adjacent issue (a factual error, a confusing structure, a missing section), mention it in your summary and offer to address it, rather than silently expanding the edit. Change code or behavior only when fixing a factual inaccuracy in the documentation itself, and call that out explicitly when you do.
