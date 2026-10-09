from backend.rag.index import build

if __name__ == "__main__":
    print("총", build(), "chunks 인덱싱 완료")
