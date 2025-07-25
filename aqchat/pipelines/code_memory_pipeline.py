import os
import shutil
from pathlib import Path
from threading import Lock
from typing import Iterable, List, Dict, Any
from langchain_core.documents import Document

from langchain_community.document_loaders import TextLoader
from langchain_chroma.vectorstores import Chroma
from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_ollama import OllamaEmbeddings
from langchain_community.vectorstores.utils import filter_complex_metadata
from pipelines.abstract_memory import AbstractMemoryPipeline
from pipelines.detectors import CodeBoundaryDetector, PythonBoundaryDetector, RustBoundaryDetector
from pipelines.boundary_splitter import CodeBoundaryTextSplitter

class CodeMemoryPipeline(AbstractMemoryPipeline):
    """Retrieval-augmented Q&A over a local Git repository (or any code directory).
    
    Uses ChromaDB for embedding vector stores.

    * Supports incremental updates (re-ingesting only changed files).
    * Persists its Chroma vector store to disk so that state survives restarts.
    * Allows streaming responses via ``query``.

    NOTE: This class MUST be thread-safe as streamlit runs multiple worker threads
    and the pipeline for a given repo will be cached, meaning several users
    having sessions may result in concurrent accesses.
    """

    # --------------------------------------------------------------
    # INITIALISATION
    # --------------------------------------------------------------

    def __init__(
        self,
        *,
        chunk_size: int = 800,
        chunk_overlap: int = 120,
        include_ext: Iterable[str] | None = None,
        persist_directory: os.PathLike | str = "/app/data/chroma",
        ollama_url: str | None = None,
        ollama_embedding_model: str | None = None,
        retrieval_settings: Dict[str, Any] = {
            "ret_strat": "mmr",
            "k": 6,
            "fetch_k": 20,
            "lambda_mult": 0.5
        },
    ) -> None:
        # allocate mutex
        self.lock = Lock()

        self.retrieval_settings = retrieval_settings

        # Keep blocks coherent when splitting
        self.text_splitter = CodeBoundaryTextSplitter(strip_whitespace=False)

        self.boundary_detectors: Dict[str, CodeBoundaryDetector] = {
            ".py": PythonBoundaryDetector(),
            ".rs": RustBoundaryDetector()
        }

        # Which file extensions to ingest
        self.include_ext = set(
            include_ext
            or {
                ".md",
                ".rst",
                ".txt",
                ".json",
                ".toml",
                ".cfg",
                ".yaml",
                ".yml",
                *self.boundary_detectors.keys(),
            }
        )

        # Persistence ‑‑ ensure directory exists, then attempt to load store
        self.persist_directory = Path(persist_directory).expanduser()
        self.persist_directory.mkdir(parents=True, exist_ok=True)

        # Runtime attributes – filled in lazily
        self.vector_store: Chroma | None = None
        self.retriever = None
        self._repo_root: Path | None = None

        if ollama_url is not None:
            self.embeddings = OllamaEmbeddings(model=ollama_embedding_model, base_url=ollama_url)
        else:
            self.embeddings = FastEmbedEmbeddings()

        # Try restoring a previously‑saved Chroma collection (if present)
        if any(self.persist_directory.iterdir()):
            try:
                self.vector_store = Chroma(
                    persist_directory=str(self.persist_directory),
                    embedding_function=self.embeddings,
                )
                self._build_chain()  # sets up self.retriever
            except Exception as ex:
                print(f"WARNING: Could not initialize vector stores from disk: {ex}")
                
                # Corrupt or incompatible store – start fresh
                self.vector_store = None
                self.retriever = None

    # --------------------------------------------------------------
    # INGESTION / INDEXING
    # --------------------------------------------------------------

    def ingest(self, repo_path: str | os.PathLike) -> None:
        """Walk *repo_path*, index eligible files, and build the retrieval chain."""
        with self.lock:
            repo_path = Path(repo_path).expanduser().resolve()
            if not repo_path.exists():
                raise FileNotFoundError(f"{repo_path} does not exist")

            self._repo_root = repo_path  # remember for later updates

            docs = self._load_repo(repo_path)
            chunks = self.text_splitter.split_documents(docs, boundary_detectors=self.boundary_detectors)
            chunks = filter_complex_metadata(chunks)

            # Filter out chunks with content length <= 3
            chunks = [chunk for chunk in chunks if len(chunk.page_content.strip()) > 3]

            # (Re)‑create vector store on disk
            self.vector_store = Chroma.from_documents(
                documents=chunks,
                embedding=self.embeddings,
                persist_directory=str(self.persist_directory),
            )

            # Build retriever and QA chain
            self._build_chain()

    # --------------------------------------------------------------
    # UPDATE PATH(S)
    # --------------------------------------------------------------

    def update_files(self, *file_paths: str | os.PathLike) -> None:
        """(Re)-index *file_paths* that were modified since the last ingest."""
        with self.lock:
            if not self.vector_store:
                raise RuntimeError("Call .ingest(<repo>) before updating files.")

            # Normalise all paths – store as *relative* paths for metadata look‑ups
            normalised: list[Path] = []
            for p in file_paths:
                p = Path(p).expanduser().resolve()
                if p.is_absolute():
                    try:
                        p = p.relative_to(self._repo_root)  # type: ignore[arg-type]
                    except ValueError:
                        raise ValueError(f"{p} is outside the ingested repository root")
                normalised.append(p)

            # Delete old chunks from the vector store where source matches
            for rel_path in normalised:
                self.vector_store.delete(where={"source": str(rel_path)})

            # (Re)‑load and split fresh content
            docs: List = []
            for rel_path in normalised:
                abs_path = self._repo_root / rel_path  # type: ignore[operator]
                if not abs_path.exists():
                    # File has been deleted – nothing more to do
                    continue
                docs.extend(self._load_single_file(abs_path, rel_path))

            if docs:
                chunks = self.text_splitter.split_documents(docs, boundary_detectors=self.boundary_detectors)
                chunks = filter_complex_metadata(chunks)

                # Filter out chunks with content length <= 3
                chunks = [chunk for chunk in chunks if len(chunk.page_content.strip()) > 3]

                self.vector_store.add_documents(chunks)

            # Refresh retriever so it sees the latest state
            self._build_chain()

    # --------------------------------------------------------------
    # PUBLIC API
    # --------------------------------------------------------------

    def ready_for_retrieval(self) -> bool:
        """Returns True if the pipeline is ready to be used."""
        with self.lock:
            return self.retriever is not None

    def invoke(self, input: str) -> List[Document]:
        """Stream an answer for *messages*.

        ``messages`` must be a list of chat messages of the form
        ``[{"role": "user" | "assistant" | "system", "content": "..."}, ...]``.
        The final user message is treated as the question for retrieval.
        """
        with self.lock:
            return self.retriever.invoke(input)
        
    def _clear(self) -> None:
        """Clear *in-memory* state - keeps the persisted DB intact."""
        self.vector_store = None
        self.retriever = None
        self.chain = None
        self._repo_root = None

    def clear(self) -> None:
        """Clear *in-memory* state - keeps the persisted DB intact."""
        with self.lock:
            self._clear()

    def has_vector_db(self) -> bool:
        """Return True if there is a loaded vector store present."""
        with self.lock:
            return self.vector_store is not None

    def clear_vector_db(self) -> None:
        """Delete the persisted Chroma database on disk and reset state."""
        with self.lock:
            self._clear()
            if self.persist_directory.exists():
                shutil.rmtree(self.persist_directory)
                self.persist_directory.mkdir(parents=True, exist_ok=True)

    def set_retrieval_settings(self, retrieval_settings: Dict[str, Any]) -> None:
        """Update retrieval settings.
        
        Retrieval settings MUST be a dict of the form:

        ```
        {
            "ret_strat": "mmr", "k": 6, "fetch_k": 20, "lambda_mult": 0.5
        }
        ```

        OR:
        ```
        {
            "ret_strat": "similarity", "k": 4
        }
        ```
        """
        with self.lock:
            self.retrieval_settings = retrieval_settings
            self._build_chain()

    # --------------------------------------------------------------
    # INTERNAL HELPERS
    # --------------------------------------------------------------

    def _build_chain(self) -> None:
        """(Re)-create the retriever after any vector-store change."""
        if not self.vector_store:
            raise RuntimeError("Vector store must be initialised before building the retriever.")
        
        ret_strat = self.retrieval_settings["ret_strat"]

        if ret_strat == "mmr":
            search_settings = {
                "k": self.retrieval_settings["k"],
                "fetch_k": self.retrieval_settings["fetch_k"],
                "lambda_mult": self.retrieval_settings["lambda_mult"]
            }
        elif ret_strat == "similarity":
            # omit other settings in case similarity is selected
            search_settings = {
                "k": self.retrieval_settings["k"]
            }
        else:
            ret_strat = None
            search_settings = None

        self.retriever = self.vector_store.as_retriever(
            search_type=ret_strat,
            search_kwargs=search_settings,
        )

    # ----------------- File‑system helpers ---------------------------

    def _load_repo(self, root: Path) -> List:
        """Return LangChain Documents for every eligible file in *root*."""
        docs = []
        ignore_dirs = {".git", ".venv", "__pycache__", "dist", "build", ".idea"}

        for path in root.rglob("*"):
            if path.is_dir() and path.name in ignore_dirs:
                continue
            if path.is_file() and path.suffix.lower() in self.include_ext:
                docs.extend(self._load_single_file(path, path.relative_to(root)))
        return docs

    def _load_single_file(self, abs_path: Path, rel_path: Path) -> List:
        """Load *abs_path* and return a list with its LangChain Document(s)."""
        loader = TextLoader(str(abs_path), encoding="utf-8")
        docs: List = []
        for doc in loader.load():
            doc.metadata["source"] = str(rel_path)
            docs.append(doc)
        return docs
