#!/usr/bin/env python3
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.settings import Settings
from app.services.embeddings import build_embedder


def validate_embeddings():
    print("=" * 70)
    print("AWS Titan Embedding Model Validation")
    print("=" * 70)

    print("\n[1] Loading configuration...")
    try:
        settings = Settings()
        print(f"  ✓ Settings loaded from environment")
        print(f"    - Embedding Provider: {settings.embedding_provider}")
        print(f"    - Bedrock Model: {settings.bedrock_embedding_model}")
        print(f"    - Expected Dimension: {settings.embedding_dim}")
        print(f"    - AWS Region: {settings.aws_region}")
        print(f"    - Credentials: {'Explicit' if settings.aws_access_key_id else 'Default chain'}")
    except Exception as e:
        print(f"  ✗ Failed to load settings: {e}")
        return False

    print("\n[2] Initializing embedding model...")
    try:
        embedder = build_embedder(settings)
        print(f"  ✓ Embedder initialized")
        print(f"    - Provider: {embedder.name}")
        print(f"    - Dimension: {embedder.dimension}")
    except Exception as e:
        print(f"  ✗ Failed to initialize embedder: {e}")
        print(f"    This usually means AWS credentials are missing or invalid.")
        print(f"    Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY or rely on IAM role.")
        return False

    print("\n[3] Validating dimensions...")
    if embedder.dimension == settings.embedding_dim:
        print(f"  ✓ Dimension match: {embedder.dimension}")
    else:
        print(f"  ✗ Dimension mismatch!")
        print(f"    - Expected: {settings.embedding_dim}")
        print(f"    - Actual: {embedder.dimension}")
        return False

    print("\n[4] Testing document embedding...")
    test_docs = [
        "Invoice from Vendor ABC for $1,500",
        "Purchase order number PO-2024-001",
        "The total amount due is $2,345.67",
    ]
    try:
        embeddings = embedder.embed_documents(test_docs)
        print(f"  ✓ Successfully embedded {len(embeddings)} documents")
        for i, emb in enumerate(embeddings):
            print(f"    - Doc {i+1}: {len(emb)} dimensions, range [{min(emb):.6f}, {max(emb):.6f}]")
    except Exception as e:
        print(f"  ✗ Failed to embed documents: {e}")
        return False

    print("\n[5] Testing query embedding...")
    test_query = "What are the invoice amounts?"
    try:
        query_emb = embedder.embed_query(test_query)
        print(f"  ✓ Successfully embedded query")
        print(f"    - Dimension: {len(query_emb)}")
        print(f"    - Value range: [{min(query_emb):.6f}, {max(query_emb):.6f}]")
    except Exception as e:
        print(f"  ✗ Failed to embed query: {e}")
        return False

    print("\n[6] Checking embedding consistency...")
    if len(embeddings[0]) == len(query_emb):
        print(f"  ✓ Document and query embeddings have same dimension: {len(embeddings[0])}")
    else:
        print(f"  ✗ Dimension mismatch between documents and query!")
        print(f"    - Documents: {len(embeddings[0])}")
        print(f"    - Query: {len(query_emb)}")
        return False

    print("\n" + "=" * 70)
    print("✓ All validations passed!")
    print("=" * 70)
    print("\nThe AWS Titan embedding model is correctly configured and operational.")
    print(f"Model: {embedder.name}")
    print(f"Region: {settings.aws_region}")
    print(f"Dimension: {embedder.dimension}")
    return True


if __name__ == "__main__":
    success = validate_embeddings()
    sys.exit(0 if success else 1)
