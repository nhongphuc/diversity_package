"""Module for calculating weighted subcommunity and metacommunity similarities.

Classes
-------
Similarity
    Abstract base class for relative abundance-weighted species
    similarities.
SimilarityFromDataFrame
    Implements Similarity by storing similarities in a pandas DataFrame.
SimilarityFromArray
    Implements Similarity by storing similarities in a numpy ndarray or memmap.
SimilarityFromFile
    Implements Similarity by reading similarities from a csv or tsv file.
SimilarityFromFunction:
    Implements Similarity by calculating pairwise similarities with a callable function.

Functions
---------
make_similarity
    Chooses and creates instances of concrete Similarity implementations.
"""
from abc import ABC, abstractmethod
from typing import Callable
from types import FunctionType
from numpy import dtype, ndarray, memmap, empty, concatenate
from pandas import DataFrame, read_csv
from ray import remote, get, put
from diversity.log import LOGGER
from diversity.utilities import get_file_delimiter


class Similarity(ABC):
    """Interface for classes computing weighted similarities."""

    @abstractmethod
    def weighted_similarities(self, relative_abundances) -> ndarray:
        """Calculates weighted sums of similarities to each species.

        Parameters
        ----------
        relative_abundances:
            Array of shape (n_species, n_communities), where rows
            correspond to unique species, columns correspond to
            (meta-/sub-) communities and each element is the relative
            abundance of a species in a (meta-/sub-)community.

        Returns
        -------
        A 2-d numpy.ndarray of shape (n_species, n_communities), where
        rows correspond to unique species, columns correspond to
        (meta-/sub-) communities and each element is a sum of
        similarities of all species to the row's species weighted by
        their relative abundances in the respective communities.
        """
        pass


class SimilarityFromDataFrame(Similarity):
    """Implements Similarity using similarities stored in pandas dataframe."""

    def __init__(self, similarity: DataFrame):
        """
        similarity:
            Similarities between species. Columns and index must be
            species names corresponding to the values in their rows and
            columns.
        """
        LOGGER.debug("SimilarityFromFile(similarity=%s", similarity)
        self.similarity: DataFrame = similarity

    def weighted_similarities(self, relative_abundances: ndarray) -> ndarray:
        return self.similarity.to_numpy() @ relative_abundances


class SimilarityFromArray(Similarity):
    """Implements Similarity using similarities stored in a numpy ndarray."""

    def __init__(self, similarity: ndarray | memmap) -> None:
        """
        similarity:
            A pairwise similarity matrix of shape (n_species, n_species) where
            each value is the similarity between a pair of species. Species must
            be in the same order as in the counts argument of
            the Metacommunity class.

        """
        LOGGER.debug("SimilarityFromFile(similarity=%s", similarity)
        self.similarity: ndarray = similarity

    def weighted_similarities(self, relative_abundances: ndarray) -> ndarray:
        return self.similarity @ relative_abundances


class SimilarityFromFile(Similarity):
    """Implements Similarity by using similarities stored in file.

    Similarity matrix rows are read from the file one chunk at a time.
    The size of chunks can be specified in numbers of rows to control
    memory load.
    """

    def __init__(self, similarity: str, chunk_size: int = 100) -> None:
        """
        Parameters
        ----------
        similarity:
            Path to a file containing a pairwise similarity matrix of
            shape (n_species, n_species). The file should have a header
            that denotes the unique species names.
        chunk_size:
            Number of rows to read from similarity matrix at a time.
        """
        LOGGER.debug(
            "SimilarityFromFile(similarity=%s, chunk_size=%s",
            similarity,
            chunk_size,
        )
        self.similarity: str = similarity
        self.chunk_size: int = chunk_size

    def weighted_similarities(self, relative_abundances: ndarray) -> ndarray:
        weighted_similarities = empty(relative_abundances.shape, dtype=dtype("f8"))
        with read_csv(
            self.similarity,
            delimiter=get_file_delimiter(self.similarity),
            chunksize=self.chunk_size,
        ) as similarity_matrix_chunks:
            i = 0
            for chunk in similarity_matrix_chunks:
                weighted_similarities[i : i + self.chunk_size, :] = (
                    chunk.to_numpy() @ relative_abundances
                )
                i += self.chunk_size
        return weighted_similarities


@remote
def weighted_similarity_chunk(
    similarity: Callable,
    X: ndarray,
    relative_abundances: ndarray,
    chunk_size: int,
    i: int,
) -> ndarray:
    chunk = X[i : i + chunk_size]
    similarities_chunk = empty(shape=(chunk.shape[0], X.shape[0]))
    for i, row_i in enumerate(chunk):
        for j, row_j in enumerate(X):
            similarities_chunk[i, j] = similarity(row_i, row_j)
    return similarities_chunk @ relative_abundances


class SimilarityFromFunction(Similarity):
    """Implements Similarity by calculating similarities with a callable function."""

    def __init__(self, similarity: Callable, X: ndarray, chunk_size: int = 100) -> None:
        """
        similarity:
            A Callable that calculates similarity between a pair of species. Must take
            two rows from X and return a numeric similarity value.
        X:
            An array where each row contains the feature values for a given species.
        chunk_size:
            Determines how many rows of the similarity matrix each will be processes at a time.
            In general, choosing a larger chunk_size will make the calculation faster,
            but will also require more memory.
        """
        LOGGER.debug(
            "SimilarityFromFile(similarity=%s, X=%s, chunk_size=%s",
            similarity,
            X,
            chunk_size,
        )
        self.similarity: Callable = similarity
        self.X: ndarray = X
        self.chunk_size: int = chunk_size

    def weighted_similarities(self, relative_abundances: ndarray) -> ndarray:
        X_ref = put(self.X)
        abundance_ref = put(relative_abundances)
        futures = [
            weighted_similarity_chunk.remote(
                similarity=self.similarity,
                X=X_ref,
                relative_abundances=abundance_ref,
                chunk_size=self.chunk_size,
                i=i,
            )
            for i in range(0, self.X.shape[0], self.chunk_size)
        ]
        weighted_similarity_chunks = get(futures)
        return concatenate(weighted_similarity_chunks)


def make_similarity(
    similarity: DataFrame | ndarray | str, X: ndarray = None, chunk_size: int = 100
) -> Similarity:
    """Initializes a concrete subclass of Similarity.

    Parameters
    ----------
    similarity:
        If pandas.DataFrame, see diversity.similarity.SimilarityFromDataFrame.
        If numpy.ndarray, see diversity.similarity.SimilarityFromArray.
        If str, see diversity.similarity.SimilarityFromFile.
        If Callable, see diversity.similarity.SimilarityFromFunction
    X:
        A 2-d array where each row is a species
    chunk_size:
        See diversity.similarity.SimilarityFromFile. Only relevant
        if a str is passed as argument for similarity.

    Returns
    -------
    An instance of a concrete subclass of Similarity.
    """
    LOGGER.debug(
        "make_similarity(similarity=%s, X=%s, chunk_size=%s)",
        similarity,
        X,
        chunk_size,
    )
    match similarity:
        case None:
            return None
        case DataFrame():
            return SimilarityFromDataFrame(similarity=similarity)
        case ndarray() | memmap():
            return SimilarityFromArray(similarity=similarity)
        case str():
            return SimilarityFromFile(similarity=similarity, chunk_size=chunk_size)
        case FunctionType():
            return SimilarityFromFunction(
                similarity=similarity, X=X, chunk_size=chunk_size
            )
        case _:
            raise NotImplementedError(
                f"Type {type(similarity)} is not supported for argument 'similarity'."
                "Valid types include pandas.DataFram, numpy.ndarray, numpy.memmap,"
                " str, or typing.Callable"
            )
