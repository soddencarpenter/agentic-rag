
import ast
import httpx
import os
import warnings

from io import BytesIO
from app.indexing.schemas import File, CodeElement
from urllib.parse import urlparse
from typing import Iterable, List, Optional, Tuple
from zipfile import ZipFile

BASE_URL = "https://codeload.github.com"
MAX_FILE_BYTES = 1_000_000  # 1 MB cap per file (adjust)
DEFAULT_EXTS = {".py", ".md"}


class GitHubParser:
    """Parser for extracting and processing code from GitHub repositories.

    This class downloads GitHub repositories as ZIP files and parses their contents
    into structured CodeElement objects suitable for indexing and retrieval.

    Attributes:
        owner (str): GitHub repository owner/organization name
        repo (str): Repository name
        ref (str | None): Git reference (branch, tag, or commit), None for default branch

    Example:
        parser = GitHubParser("https://github.com/owner/repo/tree/main")
        code_elements = parser.parse_repo()

    Note:
        Supports Python (.py) and Markdown (.md) files only.
        Files larger than MAX_FILE_BYTES are skipped.
    """

    def __init__(self, github_url):
        self.owner, self.repo, self.ref = self.parse_url(github_url)

    def parse_url(self, url: str) -> tuple[str, str, str | None]:
        """Parse a GitHub URL to extract owner, repository name, and optional reference.

        Args:
            url: GitHub URL (e.g., 'https://github.com/owner/repo' or 'https://github.com/owner/repo/tree/branch')

        Returns:
            Tuple containing (owner, repo, ref) where ref is None if not specified in URL

        Raises:
            ValueError: If URL is not a valid GitHub URL or doesn't contain owner/repo
        """
        owner, repo, ref = "", "", None

        if not url:
            return owner, repo, ref

        parsed = urlparse(url)

        if parsed.netloc.lower() != "github.com":
            raise ValueError(
                f"Invalid GitHub URL: expected 'github.com', got '{parsed.netloc}'"
            )

        # Remove leading/trailing slashes and split into path parts
        parts = [p for p in parsed.path.strip("/").split("/") if p]

        if len(parts) < 2:
            raise ValueError(
                f"Invalid GitHub URL: missing owner and repository in '{url}'"
            )

        owner = parts[0]
        repo = parts[1].removesuffix(".git")
        ref: Optional[str] = None

        # Check for /tree/<ref> or /blob/<ref>
        if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
            ref = parts[3]

        return owner, repo, ref



    def validate_owner_and_repo(self) -> None:
        """
        Ensures that owner and repo are set; raises a ConnectionError if they are not
        """
        if not getattr(self, "owner", None) or not getattr(self, "repo", None):
            raise ConnectionError("Repository owner and name must be set before fetching ZIP.")



    def obtain_refs_for_retrieve(self) -> List[str]:
        """
        Uses the ref or main/master for attempting retrieval

        refs to try:
        - If self.ref is set, try that first.
        - If not, try 'main' then 'master' (common defaults).

        """
        # Determine which refs to try:
        # - If self.ref is set, try that first.
        # - If not, try 'main' then 'master' (common defaults).
        refs_to_try: List[str] = []
        if getattr(self, "ref", None):
            refs_to_try.append(self.ref)  # type: ignore[arg-type]
        else:
            refs_to_try.extend(["main", "master"])

        return refs_to_try



    def fetch_repo_zip(self, timeout: float = 60.0) -> bytes:
        """Download this GitHub repository as a ZIP file.

        Args:
            timeout: Request timeout in seconds (default: 60.0)

        Returns:
            Raw ZIP file content as bytes

        Raises:
            ConnectionError: If repository cannot be downloaded (not found, private, or network error)

        Note:
            Uses the repository's owner, repo, and ref attributes set during initialization.
            If no ref is specified, tries 'main' then 'master' branches.
        """
        # For each ref, try URL patterns that work on codeload:
        # 1) Branches:   /zip/refs/heads/<ref>
        # 2) Tags:       /zip/refs/tags/<ref>
        # (Some setups also serve /zip/<ref>, but heads/tags are the canonical forms.)
        def candidate_urls(ref: str) -> Iterable[str]:
            owner = self.owner  # type: ignore[attr-defined]
            repo = self.repo    # type: ignore[attr-defined]
            yield f"{base_url}/{owner}/{repo}/zip/refs/heads/{ref}"
            yield f"{base_url}/{owner}/{repo}/zip/refs/tags/{ref}"


        # ensure we have some basic settings
        self.validate_owner_and_repo()

        # set the base_url
        base_url: str = BASE_URL.rstrip("/")

        # what are we trying to retrieve
        refs_to_try = self.obtain_refs_for_retrieve()

        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                for ref in refs_to_try:
                    for url in candidate_urls(ref):
                        resp = client.get(url)
                        if resp.status_code == 200 and resp.content:
                            return resp.content
                        # Try next candidate on 3xx/4xx/5xx or empty payload

        except httpx.RequestError as exc:
            raise ConnectionError(f"Network error while downloading ZIP: {exc}") from exc

        # If we got here, none of the candidates succeeded.
        raise ConnectionError("Could not download ZIP (ref not found or repo private).")



    def extract_prefix(self, zip_file:ZipFile) -> str:
        """
        From a zip file, extract a prefix based upon
        all of the anmes in the zip file
        """
        # Compute common root prefix across all entries
        all_names = [info.filename for info in zip_file.infolist()]
        # Strip trailing slashes so commonpath works robustly
        normalized = [name.rstrip("/") for name in all_names if name]
        common_root = os.path.commonpath(normalized) if normalized else ""
        return f"{common_root}/" if common_root and not common_root.endswith("/") else common_root


    def is_entry_processable(self,
                             prefix:str,
                             max_bytes:int,
                             info
    ) -> bool:
        """
        Checks to see if the info (an entry in a .zip file) should
        be process. We do not process:
        - directories
        - things over max_bytes
        - entires that do not have the prefix
        """

        # Skip directories outright
        if info.is_dir():
            return False

        # Ensure entry begins with the prefix (if any)
        if prefix and not info.filename.startswith(prefix):
            return False

        # Enforce size limit
        if info.file_size > max_bytes:
            return False

        return True



    def get_files_from_zip(
        self,
        zip_bytes: bytes,
        max_bytes: int = MAX_FILE_BYTES
    ) -> List[File]:
        """Extract and process files from a ZIP archive.

        Args:
            zip_bytes: Raw ZIP file content as bytes
            max_bytes: Maximum file size in bytes to process (default: MAX_FILE_BYTES)

        Returns:
            List of File objects containing content, path, and extension for each processed file

        Note:
            Only processes files with extensions in DEFAULT_EXTS (.py, .md).
            Skips directories and files exceeding max_bytes limit.
            Text encoding falls back from UTF-8 to Latin-1 if decoding fails.
        """
        files: List[File] = []

        with ZipFile(BytesIO(zip_bytes)) as zip_file:
            prefix = self.extract_prefix(zip_file)

            for info in zip_file.infolist():
                if not self.is_entry_processable(prefix, max_bytes, info):
                    continue

                # Compute path relative to the common prefix
                path = info.filename[len(prefix):] if prefix else info.filename
                # Ignore empty/invalid paths (paranoia)
                if not path:
                    continue

                # Filter by extension
                extension = os.path.splitext(path)[1].lower()
                if extension not in DEFAULT_EXTS:
                    continue

                # Read and decode file contents with fallback
                with zip_file.open(info) as f:
                    raw = f.read()
                    try:
                        text = raw.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        text = raw.decode("latin-1", errors="replace").strip()

                files.append(File(content=text, path=path, extension=extension))

        return files


    def _slice_node(self, lines: list[str], node: ast.AST) -> list[str]:
        """Extract source lines that correspond to a given AST node."""
        if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
            return []
        start = max(1, getattr(node, "lineno", 1)) - 1
        end = getattr(node, "end_lineno", start + 1)
        return lines[start:end]



    def _class_header_lines(self, node: ast.ClassDef, lines: list[str]) -> list[str]:
        """
        Return the header lines for the class (from 'class ...' to the line
        before the first body item starts). If the class is empty, the header
        is just the class line (and any decorators captured by lineno range).
        """
        if node.body:
            first_body_line = getattr(node.body[0], "lineno", node.lineno + 1)
        else:
            first_body_line = node.lineno + 1
        start = node.lineno - 1
        end_exclusive = max(start, first_body_line - 1)  # slice end (exclusive)
        return lines[start:end_exclusive]



    def _class_blocks(self, node: ast.ClassDef, lines: list[str]) -> list[list[str]]:
        """
        Return method-aligned (and trailing) blocks for the class.
        Each block is a list[str] of contiguous source lines corresponding
        to a single top-level node (method/attribute/stmt) or trailing text.
        """
        blocks: list[list[str]] = []
        last_end = None

        for item in node.body:
            block = self._slice_node(lines, item)
            if block:
                blocks.append(block)
            last_end = getattr(item, "end_lineno", last_end)

        # Trailing lines after the last body node up to class end
        node_end = getattr(node, "end_lineno", None)
        if last_end is not None and node_end is not None and last_end < node_end:
            trailing = lines[last_end:node_end]
            if trailing:
                blocks.append(trailing)

        return blocks



    def _pack_parts(
        self,
        *,
        header_lines: list[str],
        blocks: list[list[str]],
        max_lines: int,
        class_name: str,
    ) -> list[str]:
        """
        Greedily pack blocks into parts with a line budget.
        - First part uses the real header.
        - Continuation parts repeat 'class Name:' and include '    ...'.
        - Methods/blocks are never split.
        """
        if not blocks:
            # No body: still return a single part with the header only
            return ["\n".join(header_lines).rstrip()]

        parts: list[str] = []
        current: list[str] = []
        remaining = max_lines

        def commit() -> None:
            if current:
                parts.append("\n".join(current).rstrip())
                current.clear()

        def start_first_part() -> None:
            nonlocal remaining
            current.extend(header_lines)
            remaining = max_lines - len(header_lines)

        def start_continuation() -> None:
            nonlocal remaining
            # Repeat minimal header + continuation marker
            class_line = header_lines[0].rstrip() if header_lines else f"class {class_name}:"
            current.append(class_line)
            current.append("    ...")
            remaining = max_lines - 2

        # Initialize first part with the real header
        start_first_part()

        for block in blocks:
            blen = len(block)
            if blen > max_lines:
                # Oversized single block: put it alone in a continuation part
                commit()
                start_continuation()
                current.extend(block)
                commit()
                continue

            if blen > remaining:
                commit()
                start_continuation()

            current.extend(block)
            remaining -= blen

        commit()
        return parts



    def _split_class(self, node: ast.ClassDef, *, lines: list[str], max_lines: int) -> list[str]:
        """
        Low-complexity splitter for class definitions:
        - Never splits methods/blocks.
        - Repeats 'class Name:' and adds '    ...' for continuations.
        - Returns bounded parts (≤ max_lines each), or a single part if small.
        """
        full = self._slice_node(lines, node)
        if not full:
            return []

        header_lines = self._class_header_lines(node, lines)
        blocks = self._class_blocks(node, lines)

        parts = self._pack_parts(
            header_lines=header_lines,
            blocks=blocks,
            max_lines=max_lines,
            class_name=node.name,
        )

        # Fallback (shouldn’t happen, but safe): return whole class text
        return parts or ["\n".join(full).rstrip()]



    def parse_code(self, file: File, *, max_lines_per_elem: int = 300) -> list[CodeElement]:
        """Parse Python code into structured CodeElement objects with intelligent chunking.

        Args:
            file: File object containing Python source code to parse
            max_lines_per_elem: Maximum lines per code element before splitting (default: 200)

        Returns:
            List of CodeElement objects, each containing logically grouped code chunks

        Note:
            Intelligently splits large classes into multiple elements while preserving context.
            Groups related functions and maintains header context with imports/globals.
            Uses AST parsing to respect Python structure rather than arbitrary line splits.
        """
        def rstrip_blanks(seq: list[str]) -> list[str]:
            end = len(seq)
            while end > 0 and seq[end - 1].strip() == "":
                end -= 1
            return seq[:end]

        def emit_prev() -> None:
            nonlocal prev_lines, prev_len
            if prev_lines:
                text = "\n".join(rstrip_blanks(prev_lines))
                header_text = "\n".join(rstrip_blanks(headers))
                code_elements.append(
                    CodeElement(
                        text=text,
                        source=file.path,
                        header=header_text,
                        extension=file.extension,
                        description=None,
                    )
                )
                prev_lines = []
                prev_len = 0


        try:
            with warnings.catch_warnings():
                # Some Python versions used DeprecationWarning; current ones use SyntaxWarning
                warnings.simplefilter("ignore", SyntaxWarning)
                warnings.simplefilter("ignore", DeprecationWarning)
                module = ast.parse(file.content, filename=file.path)
        except Exception:
            return []


        lines = file.content.splitlines()

        headers: list[str] = []
        code_elements: list[CodeElement] = []
        prev_lines: list[str] = []
        prev_len = 0

        for node in module.body:
            node_text = self._slice_node(lines, node)

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_len = len(node_text)
                if prev_len > 0 and prev_len + fn_len > max_lines_per_elem:
                    emit_prev()
                if prev_len > 0:
                    prev_lines.append("")
                    prev_len += 1
                prev_lines.extend(node_text)
                prev_len += fn_len

            elif isinstance(node, ast.ClassDef):
                parts = self._split_class(node, lines=lines, max_lines=max_lines_per_elem)
                for part in parts:
                    part_lines = part.splitlines()
                    part_len = len(part_lines)
                    if prev_len > 0 and prev_len + part_len > max_lines_per_elem:
                        emit_prev()
                    if prev_len > 0:
                        prev_lines.append("")
                        prev_len += 1
                    prev_lines.extend(part_lines)
                    prev_len += part_len

            else:
                headers.extend(node_text)

        emit_prev()

        if not code_elements:
            whole = "\n".join(rstrip_blanks(lines))
            code_elements.append(
                CodeElement(
                    text=whole,
                    source=file.path,
                    header="",
                    extension=file.extension,
                    description=None,
                )
            )

        return code_elements


    def parse_markdown(
        self,
        file: File,
        *,
        min_lines_per_elem: int = 100,
        overlap_lines: int = 5,
    ) -> list[CodeElement]:
        """Parse Markdown content into overlapping CodeElement chunks.

        Args:
            file: File object containing Markdown content to parse.
            min_lines_per_elem: Lines per chunk (default: 100).
            overlap_lines: Number of overlapping lines between chunks (default: 5).

        Returns:
            List of CodeElement objects with chunked Markdown content.

        Note:
            Creates overlapping chunks to preserve context across boundaries.
            Step size = min_lines_per_elem - overlap_lines to ensure forward progress.
            Overlap is clamped to be less than chunk size to avoid infinite loops.
        """
        if min_lines_per_elem <= 0:
            raise ValueError("min_lines_per_elem must be greater than 0")

        source = file.path
        extension = file.extension
        lines = file.content.splitlines(keepends=True)
        num_lines = len(lines)

        # Clamp overlap_lines to [0, min_lines_per_elem - 1]
        overlap_lines = max(0, min(overlap_lines, min_lines_per_elem - 1))

        # Step = min_lines_per_elem - overlap_lines (always ≥ 1)
        step = min_lines_per_elem - overlap_lines

        chunks: list[CodeElement] = []

        for start in range(0, num_lines, step):
            end = min(start + min_lines_per_elem, num_lines)
            text = "".join(lines[start:end])

            if not text.strip():
                continue  # skip empty chunks

            chunks.append(
                CodeElement(
                    text=text,
                    source=source,
                    header="",  # Markdown chunks have no header
                    extension=extension,
                    description=None,
                )
            )

            if end >= num_lines:
                break  # stop when we've reached the end

        return chunks


    def parse_repo(self) -> list[CodeElement]:
        """Parse the GitHub repository into structured code elements.

        Returns:
            List of CodeElement objects containing parsed content from Python and Markdown files

        Note:
            Downloads the repository ZIP using instance attributes (owner, repo, ref).
            Processes .py files using AST parsing and .md files using chunk-based parsing.
            Filters files based on DEFAULT_EXTS and MAX_FILE_BYTES limits.
        """
        # 1) Download the repository as a ZIP (may raise ConnectionError)
        zip_bytes = self.fetch_repo_zip()

        # 2) Extract candidate files from the ZIP (filters by size and DEFAULT_EXTS)
        files: list[File] = self.get_files_from_zip(zip_bytes, max_bytes=MAX_FILE_BYTES)

        # 3) Parse each file according to its extension
        code_elements: list[CodeElement] = []
        for f in files:
            if f.extension == ".py":
                code_elements.extend(self.parse_code(f))
            elif f.extension == ".md":
                code_elements.extend(self.parse_markdown(f))
            # DEFAULT_EXTS already filters other extensions

        return code_elements
