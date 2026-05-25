import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.vector_store import VectorStore

DIM = 384


class TestVectorStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.index_path = os.path.join(self.tmpdir, "test.faiss")
        self.meta_path = os.path.join(self.tmpdir, "test_meta.json")
        self.store = VectorStore(
            index_path=self.index_path,
            metadata_path=self.meta_path,
            dim=DIM,
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- Basic CRUD ---

    def test_empty_store(self):
        self.assertEqual(self.store.size(), 0)
        self.assertEqual(self.store.search(np.random.randn(DIM).astype(np.float32), k=5), [])

    def test_add_and_search(self):
        vec = np.random.randn(DIM).astype(np.float32)
        norms = np.linalg.norm(vec)
        if norms > 0:
            vec = vec / norms
        self.store.add(vec, text="hello world", metadata={"detail": "test"})
        self.assertEqual(self.store.size(), 1)
        results = self.store.search(vec, k=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 0)
        self.assertEqual(results[0]["text"], "hello world")
        self.assertGreater(results[0]["score"], 0.5)

    def test_add_multiple_and_search(self):
        texts = ["apple", "banana", "cherry", "date", "elderberry"]
        vectors = []
        for t in texts:
            rng = np.random.RandomState(hash(t) % (2**31))
            v = rng.randn(DIM).astype(np.float32)
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
            vectors.append(v)
            self.store.add(v, text=t)
        self.assertEqual(self.store.size(), 5)
        results = self.store.search(vectors[0], k=5)
        self.assertEqual(len(results), 5)
        self.assertEqual(results[0]["text"], "apple")

    def test_search_k_greater_than_size(self):
        vec = np.random.randn(DIM).astype(np.float32)
        self.store.add(vec, text="only")
        results = self.store.search(vec, k=100)
        self.assertEqual(len(results), 1)

    def test_search_returns_empty_for_empty_store(self):
        results = self.store.search(np.random.randn(DIM).astype(np.float32), k=5)
        self.assertEqual(results, [])

    def test_remove_by_single_id(self):
        vec = np.random.randn(DIM).astype(np.float32)
        self.store.add(vec, text="a")
        self.store.add(vec, text="b")
        self.assertEqual(self.store.size(), 2)
        self.store.remove([0])
        self.assertEqual(self.store.size(), 1)

    def test_remove_by_multiple_ids(self):
        vec = np.random.randn(DIM).astype(np.float32)
        for i in range(5):
            self.store.add(vec, text=str(i))
        self.assertEqual(self.store.size(), 5)
        self.store.remove([0, 2, 4])
        self.assertEqual(self.store.size(), 2)

    def test_remove_nonexistent_does_not_fail(self):
        self.store.add(np.random.randn(DIM).astype(np.float32), text="a")
        self.store.remove([99])
        self.assertEqual(self.store.size(), 1)

    # --- Persistence ---

    def test_save_and_load(self):
        vec = np.random.randn(DIM).astype(np.float32)
        n = np.linalg.norm(vec)
        if n > 0:
            vec = vec / n
        self.store.add(vec, text="persist")
        self.store.save()

        store2 = VectorStore(
            index_path=self.index_path,
            metadata_path=self.meta_path,
            dim=DIM,
        )
        self.assertTrue(store2.load())
        self.assertEqual(store2.size(), 1)
        results = store2.search(vec, k=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "persist")

    def test_load_missing_files(self):
        store2 = VectorStore(
            index_path=self.index_path,
            metadata_path=self.meta_path,
            dim=DIM,
        )
        self.assertFalse(store2.load())

    def test_save_atomicity(self):
        vec = np.random.randn(DIM).astype(np.float32)
        self.store.add(vec, text="atomic")
        self.store.save()
        self.assertTrue(os.path.exists(self.index_path))
        self.assertTrue(os.path.exists(self.meta_path))

    # --- Migration ---

    def test_migrate_from_embeddings(self):
        old_embeddings = np.random.randn(3, DIM).astype(np.float32)
        old_metadata = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
        count = self.store.migrate_from_embeddings(old_embeddings, old_metadata)
        self.assertEqual(count, 3)
        self.assertEqual(self.store.size(), 3)
        results = self.store.search(old_embeddings[0], k=3)
        self.assertEqual(len(results), 3)

    def test_migrate_from_empty_embeddings(self):
        old_embeddings = np.empty((0, DIM), dtype=np.float32)
        count = self.store.migrate_from_embeddings(old_embeddings, [])
        self.assertEqual(count, 0)
        self.assertEqual(self.store.size(), 0)

    def test_migrate_mismatched_lengths(self):
        old_embeddings = np.random.randn(3, DIM).astype(np.float32)
        count = self.store.migrate_from_embeddings(old_embeddings, [{"text": "a"}])
        self.assertEqual(count, 0)
        self.assertEqual(self.store.size(), 0)

    # --- Edge Cases ---

    def test_dimension_mismatch(self):
        wrong_vec = np.random.randn(DIM + 1).astype(np.float32)
        self.store.add(wrong_vec, text="wrong")
        self.assertEqual(self.store.size(), 1)

    def test_add_empty_vector(self):
        empty = np.array([], dtype=np.float32)
        self.store.add(empty, text="empty")
        self.assertEqual(self.store.size(), 1)

    def test_clear(self):
        for i in range(5):
            self.store.add(np.random.randn(DIM).astype(np.float32), text=str(i))
        self.assertEqual(self.store.size(), 5)
        self.store.clear()
        self.assertEqual(self.store.size(), 0)

    def test_search_after_remove(self):
        vec_a = np.random.randn(DIM).astype(np.float32)
        vec_b = np.random.randn(DIM).astype(np.float32)
        for v in [vec_a, vec_b]:
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
        self.store.add(vec_a, text="a")
        self.store.add(vec_b, text="b")
        self.store.remove([0])
        self.assertEqual(self.store.size(), 1)
        self.assertEqual(self.store.metadata[0]["text"], "b")


if __name__ == "__main__":
    unittest.main()
