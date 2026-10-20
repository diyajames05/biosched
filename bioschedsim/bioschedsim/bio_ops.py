"""Phase 1 - Representative bioinformatics operations.

Four DNA-processing operations with deliberately different CPU profiles:

    gc_content     linear scan, very short            O(L)
    kmer_count     sliding window + hash table        O(L)  (larger constant, grows with k)
    motif_search   pattern search, all occurrences    O(L)  typical, depends on matches
    alignment      Needleman-Wunsch global alignment  O(L1 * L2)  (CPU-intensive)

They are written in plain Python on purpose: the goal is to obtain
realistic, *measurable* differences in execution time for the profiler,
not to be the fastest possible implementations.
"""
from __future__ import annotations

import random
from collections import Counter
from typing import Dict, Iterator, List, Tuple

DNA = "ACGT"


# ----------------------------------------------------------------- sequences
def random_dna(length: int, rng: random.Random | None = None, gc_bias: float = 0.5) -> str:
    """Random DNA sequence. ``gc_bias`` is the probability of G or C."""
    rng = rng or random.Random()
    at, gc = (1 - gc_bias) / 2, gc_bias / 2
    return "".join(rng.choices(DNA, weights=[at, gc, gc, at], k=length))


VALID_BASES = set("ACGTN")


class SequenceValidationError(ValueError):
    pass


def validate_sequence(seq: str, name: str = "sequence", allow_n: bool = True,
                      min_length: int = 1) -> str:
    """Upper-case and validate a DNA sequence.

    Rejects empty/too-short sequences and characters outside A, C, G, T
    (and N when allow_n). Returns the cleaned sequence.
    """
    s = "".join(seq.split()).upper()
    if len(s) < min_length:
        raise SequenceValidationError(f"{name}: length {len(s)} < minimum {min_length}")
    allowed = VALID_BASES if allow_n else set("ACGT")
    bad = {c for c in s if c not in allowed}
    if bad:
        raise SequenceValidationError(f"{name}: invalid characters {sorted(bad)}")
    return s


def load_sequences(path: str, min_length: int = 50, allow_n: bool = True,
                   strip_n: bool = True) -> Tuple[List[Tuple[str, str]], dict]:
    """Read + validate a FASTA file (Module 1).

    Uses Biopython's Bio.SeqIO when installed, otherwise the built-in parser.
    Records that fail validation are skipped and reported, never silently used.
    Returns (records, report).
    """
    try:
        from Bio import SeqIO  # optional dependency
        raw = [(r.description, str(r.seq)) for r in SeqIO.parse(path, "fasta")]
        parser = "Bio.SeqIO"
    except ImportError:
        raw = list(read_fasta(path))
        parser = "built-in"
    records, rejected, n_bases = [], [], 0
    for header, seq in raw:
        try:
            s = validate_sequence(seq, header, allow_n=allow_n, min_length=1)
            if strip_n:
                s = s.replace("N", "")
            validate_sequence(s, header, min_length=min_length)
            records.append((header, s))
            n_bases += len(s)
        except SequenceValidationError as e:
            rejected.append(str(e))
    report = {"file": path, "parser": parser, "records_read": len(raw),
              "records_valid": len(records), "rejected": rejected, "total_bases": n_bases,
              "gc_content": (sum(gc_content(s) * len(s) for _, s in records) / n_bases) if n_bases else 0.0}
    if not records:
        raise SequenceValidationError(f"{path}: no valid sequences ({len(rejected)} rejected)")
    return records, report


def read_fasta(path: str) -> Iterator[Tuple[str, str]]:
    """Yield (header, sequence) records from a FASTA file (minimal parser)."""
    header, chunks = None, []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if header is not None:
                    yield header, "".join(chunks).upper()
                header, chunks = line[1:], []
            else:
                chunks.append(line)
    if header is not None:
        yield header, "".join(chunks).upper()


def write_fasta(path: str, records: List[Tuple[str, str]], width: int = 70) -> None:
    with open(path, "w") as fh:
        for header, seq in records:
            fh.write(f">{header}\n")
            for i in range(0, len(seq), width):
                fh.write(seq[i:i + width] + "\n")


# ---------------------------------------------------------------- operations
def gc_content(seq: str) -> float:
    """Fraction of G + C nucleotides."""
    if not seq:
        return 0.0
    return (seq.count("G") + seq.count("C")) / len(seq)


def kmer_count(seq: str, k: int) -> Dict[str, int]:
    """Count every (overlapping) subsequence of length k."""
    return Counter(seq[i:i + k] for i in range(len(seq) - k + 1))


def motif_search(seq: str, motif: str, max_mismatches: int = 0) -> List[int]:
    """Start positions of all (overlapping) occurrences of ``motif``.

    max_mismatches = 0 -> exact search (fast, C-level str.find)
    max_mismatches > 0 -> approximate search by Hamming distance, the usual
                          setting for transcription-factor binding sites;
                          cost grows with L * m and with the match rate.
    """
    hits: List[int] = []
    if max_mismatches == 0:
        i = seq.find(motif)
        while i != -1:
            hits.append(i)
            i = seq.find(motif, i + 1)
        return hits
    m = len(motif)
    for i in range(len(seq) - m + 1):
        mism = 0
        for x, y in zip(seq[i:i + m], motif):
            if x != y:
                mism += 1
                if mism > max_mismatches:
                    break
        else:
            hits.append(i)
    return hits


def needleman_wunsch(a: str, b: str, match: int = 1, mismatch: int = -1, gap: int = -2) -> int:
    """Global alignment score (Needleman-Wunsch), linear-space DP."""
    prev = [j * gap for j in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        cur = [i * gap] + [0] * len(b)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            diag = prev[j - 1] + (match if ai == b[j - 1] else mismatch)
            up = prev[j] + gap
            left = cur[j - 1] + gap
            cur[j] = diag if diag >= up and diag >= left else (up if up >= left else left)
        prev = cur
    return prev[-1]


CATEGORIES = ("gc_content", "kmer_count", "motif_search", "alignment")


def run_operation(category: str, params: dict, seq_a: str, seq_b: str = ""):
    """Execute one bioinformatics task (used by the dataset-based profiler)."""
    if category == "gc_content":
        return gc_content(seq_a)
    if category == "kmer_count":
        return kmer_count(seq_a, params["k"])
    if category == "motif_search":
        return motif_search(seq_a, params["motif"], params.get("max_mismatches", 1))
    if category == "alignment":
        return needleman_wunsch(seq_a, seq_b)
    raise ValueError(f"unknown category {category!r}")
