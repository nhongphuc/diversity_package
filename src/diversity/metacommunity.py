"""Module for metacommunity and subcommunity diversity measures.

Classes
-------
IAbundance
    Abstract base class for relative species abundances in (meta-/sub-)
    communities.
Abundance
    Implements IAbundance for fast, but memory-heavy calculations.
SharedAbundance
    Implements IAbundance using shared memory.
ISimilarity
    Abstract base class for relative abundance-weighted species
    similarities.
SimilarityFromFile
    Implements Similarity by reading similarities from a file.
SimilarityFromMemory
    Implements Similarity by storing similarities in memory.
Metacommunity
    Represents a metacommunity made up of subcommunities and computes
    metacommunity subcommunity diversity measures.

Functions
---------
make_metacommunity
    Builds diversity.metacommunity.Metacommunity object according to
    parameter specification.
"""

from abc import ABC, abstractmethod
from functools import cached_property

from pandas import DataFrame, concat, read_csv, unique
from numpy import empty, flatnonzero, zeros, broadcast_to, divide, float64 

from diversity.log import LOGGER
from diversity.shared import SharedArraySpec
from diversity.utilities import (
    get_file_delimiter,
    InvalidArgumentError,
    pivot_table,
    power_mean,
    unique_correspondence,
)


class IAbundance(ABC):
    """Relative abundances of species in a metacommunity.

    A community consists of a set of species, each of which may appear
    any (non-negative) number of times. A metacommunity consists of one
    or more subcommunities and can be represented by the number of
    appearances of each species in each of the subcommunities that the
    species appears in.
    """

    @property
    @abstractmethod
    def subcommunity_abundance(self):
        """Calculates the relative abundances in subcommunities.

        Returns
        -------
        A numpy.ndarray of shape (n_species, n_subcommunities), where
        rows correspond to unique species, columns correspond to
        subcommunities and each element is the abundance of the species
        in the subcommunity relative to the total metacommunity size.
        """
        pass

    @property
    @abstractmethod
    def metacommunity_abundance(self):
        """Calculates the relative abundances in metacommunity.

        Returns
        -------
        A numpy.ndarray of shape (n_species, 1), where rows correspond
        to unique species and each row contains the relative abundance
        of the species in the metacommunity.
        """
        pass

    @property
    @abstractmethod
    def subcommunity_normalizing_constants(self):
        """Calculates subcommunity normalizing constants.

        Returns
        -------
        A numpy.ndarray of shape (n_subcommunities,), with the fraction
        of each subcommunity's size of the metacommunity.
        """
        pass

    @property
    @abstractmethod
    def normalized_subcommunity_abundance(self):
        """Calculates normalized relative abundances in subcommunities.

        Returns
        -------
        A numpy.ndarray of shape (n_species, n_subcommunities), where
        rows correspond to unique species, columns correspond to
        subcommunities and each element is the abundance of the species
        in the subcommunity relative to the subcommunity size.
        """
        pass


class Abundance(IAbundance):
    """Implements IAbundance for fast, but memory-heavy calculations.

    Caches counts and (normalized) relative meta- and subcommunity
    abundances at the same time.
    """

    def __init__(self, counts):
        """Initializes object.

        Parameters
        ----------
        counts: numpy.ndarray
            A 2-d numpy.ndarray with one column per subcommunity, one
            row per species, containing the count of each species in the
            corresponding subcommunities.

        """
        LOGGER.debug("Abundance(counts=%s", counts)
        self.counts = counts

    @cached_property
    def subcommunity_abundance(self):
        total_abundance = self.counts.sum()
        relative_abundances = empty(shape=self.counts.shape, dtype=float64)
        relative_abundances[:] = self.counts / total_abundance
        return relative_abundances

    @cached_property
    def metacommunity_abundance(self):
        return self.subcommunity_abundance.sum(axis=1, keepdims=True)

    @cached_property
    def subcommunity_normalizing_constants(self):
        return self.subcommunity_abundance.sum(axis=0)

    @cached_property
    def normalized_subcommunity_abundance(self):
        return self.subcommunity_abundance / self.subcommunity_normalizing_constants


class SharedAbundance(IAbundance):
    """Implements IAbundance using shared memory.

    Caches only one of relative subcommunity abundances and normalized
    relative subcommunity abundances at a time. All relative abundances
    are stored in shared arrays, which can be passed to other processors
    without copying.
    """

    counts = "count"
    subcommunity_abundances = "subcommunity_abundance"
    normalized_subcommunity_abundances = "normalized_subcommunity_abundance"

    def __init__(self, counts, shared_array_manager):
        """Initializes object.

        Parameters
        ----------
        counts: diversity.shared.SharedArrayView
            A 2-d shared array with one column per subcommunity, one
            row per species, containing the count of each species in the
            corresponding subcommunities.
        shared_array_manager: diversity.shared.SharedArrayManager
            An active manager for creating shared arrays.
        """
        LOGGER.debug("SharedAbundance(counts=%s)", counts)
        self.__shared_data = counts
        self.__shared_array_manager = shared_array_manager
        self.__shared_data.data.flags.writable = False
        self.__stores = "count"
        self.__total_abundance = self.__shared_data.data.sum()
        self.__subcommunity_normalizing_constants = (
            self.__shared_data.data.sum(axis=0) / self.__total_abundance
        )
        self.__metacommunity_abundance = None

    @cached_property
    def __spec(self):
        return SharedArraySpec(
            name=self.__shared_data.name,
            shape=self.__shared_data.data.shape,
            dtype=self.__shared_data.data.dtype,
        )

    @property
    def subcommunity_abundance(self):
        self.__shared_data.data.flags.writable = True
        if self.__stores == self.counts:
            self.__shared_data.data /= self.__total_abundance
        elif self.__stores == self.normalized_subcommunity_abundances:
            self.__shared_data.data *= self.__subcommunity_normalizing_constants
        self.__shared_data.data.flags.writable = False
        self.__stores = self.subcommunity_abundances
        return self.__shared_data.data

    @property
    def subcommunity_abundance_spec(self):
        """Memory block of data after storing relative subcommunity abundances.

        Returns
        -------
        A diversity.shared.SharedArraySpec object desribing the memory
        block at which the data is stored.
        """
        self.subcommunity_abundance
        return self.__spec

    @property
    def metacommunity_abundance(self):
        if self.__metacommunity_abundance is None:
            self.__metacommunity_abundance = self.__shared_array_manager.empty(
                shape=(self.__shared_data.data.shape[0], 1),
                dtype=self.__shared_data.data.dtype,
            )
            self.__shared_data.sum(
                axis=1, keepdims=True, out=self.__metacommunity_abundance.data
            )
            if self.__stores == self.counts:
                self.__metacommunity_abundance.data /= self.__total_abundance
            elif self.__stores == self.normalized_subcommunity_abundances:
                self.__metacommunity_abundance.data *= (
                    self.__subcommunity_normalizing_constants
                )
        return self.__metacommunity_abundance.data

    @property
    def metacommunity_abundance_spec(self):
        """Memory block of relative metacommunity abundances.

        Returns
        -------
        A diversity.shared.SharedArraySpec object desribing the memory
        block at which the data is stored.
        """
        self.metacommunity_abundance
        return self.__metacommunity_abundance.spec

    @property
    def subcommunity_normalizing_constants(self):
        return self.__subcommunity_normalizing_constants

    @property
    def normalized_subcommunity_abundance(self):
        self.__shared_data.data.flags.writable = True
        if self.__stores == self.counts:
            self.__shared_data.data /= (
                self.__total_abundance * self.__subcommunity_normalizing_constants
            )
        elif self.__stores == self.subcommunity_abundances:
            self.__shared_data.data /= self.__subcommunity_normalizing_constants
        self.__shared_data.data.flags.writable = False
        self.__stores = self.normalized_subcommunity_abundances
        return self.__shared_data.data

    @property
    def normalized_subcommunity_abundance_spec(self):
        """Memory block of data after storing relative normalized subcommunity abundances.

        Returns
        -------
        A diversity.shared.SharedArraySpec object desribing the memory
        block at which the data is stored.
        """
        self.normalized_subcommunity_abundance
        return self.__spec


class ISimilarity(ABC):
    """Interface for classes computing weighted similarities."""

    @abstractmethod
    def calculate_weighted_similarities(self, relative_abundances):
        """Calculates weighted sums of similarities to each species.

        Parameters
        ----------
        relative_abundances: numpy.ndarray
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

    @property
    @abstractmethod
    def species_order(self):
        """The ordering of species used by the object.

        Returns
        -------
        A 1-d numpy.ndarray of species names in the ordering used for
        the return value of the object's .calculate_weighted_similarities
        method.
        """
        pass


class SimilarityFromFile(ISimilarity):
    """Implements ISimilarity by using similarities stored in file.

    Similarity matrix rows are read from the file one chunk at a time.
    The size of chunks can be specified in numbers of rows to control
    memory load.
    """
    def __init__(self, similarity_matrix, species_subset, chunk_size=1): 
        """Initializes object.

        Parameters
        ----------
        similarity_matrix: str
            Path to similarities file containing a square matrix of
            similarities between species, together with a header
            containing the unique species names in the matrix's row and
            column ordering.
        species_subset: Set
            The species to include. Only similarities from columns and
            rows corresponding to these species are used.
        chunk_size: int
            Number of rows to read from similarity matrix at a time.
        """
        self.similarity_matrix = similarity_matrix
        self.__delimiter = get_file_delimiter(self.similarity_matrix)
        self.chunk_size = chunk_size
        (
            self.__species_order,
            self.__usecols,
            self.__skiprows,
        ) = self.__get_species_order(species_subset)

    def __get_species_order(self, species_subset):
        """The species ordering used in similarity matrix file.

        Parameters
        ----------
        # FIXME this may not be a set
        species: Set
            Set of species to include. If None, all species are
            included.

        Returns
        -------
        A tuple consisting of
        0 - numpy.ndarray (1d)
            Uniqued species ordered according to the similarity matrix
            file header.
        1 - numpy.ndarray (1d)
            Column numbers (0-based) of columns corresponding to members
            of species, or None if species is None.
        2 - numpy.ndarray (1d)
            Row numbers (0-based, header counts as row 0) of rows
            corresponding to non-members of species, or None if species
            is None.
        """
        LOGGER.debug("SimilarityFromFile.__get_species_order(%s, %s)" % (self, species_subset))
        with read_csv(
            self.similarity_matrix, delimiter=self.__delimiter, chunksize=1
        ) as similarity_matrix_chunks:
            species = (
                next(similarity_matrix_chunks)
                .columns
                .astype(str)
            )
        species_subset_indices = species.isin(species_subset)
        species_order = species[species_subset_indices]
        usecols = flatnonzero(species_subset_indices)
        skiprows = flatnonzero(~species_subset_indices) + 1
        return species_order, usecols, skiprows

    @cached_property
    def species_order(self):
        return self.__species_order

    def calculate_weighted_similarities(self, relative_abundances):
        weighted_similarities = empty(relative_abundances.shape, dtype=float64)
        with read_csv(
            self.similarity_matrix,
            delimiter=self.__delimiter,
            chunksize=self.chunk_size,
            usecols=self.__usecols,
            skiprows=self.__skiprows,
        ) as similarity_matrix_chunks:
            i = 0
            for chunk in similarity_matrix_chunks:
                weighted_similarities[i : i + self.chunk_size, :] = (
                    chunk.to_numpy() @ relative_abundances
                )
                i += self.chunk_size
        return weighted_similarities


class SimilarityFromMemory(ISimilarity):
    """Implements Similarity using similarities stored in memory."""

    def __init__(self, similarity_matrix, species_subset):
        """Initializes object.

        similarity_matrix: pandas.DataFrame
            Similarities between species. Columns and index must be
            species names corresponding to the values in their rows and
            columns.
        species: Set
            Set of species to include. If None, all species are
            included.
        """
        self.__species_order = self.__get_species_order(similarity_matrix, species_subset)
        self.similarity_matrix = self.__reindex_similarity_matrix(similarity_matrix)

    @property
    def species_order(self):
        return self.__species_order

    def __get_species_order(self, similarity_matrix, species_subset):
        species = similarity_matrix.columns.astype(str)
        species_subset_indices = species.isin(species_subset)
        return species[species_subset_indices]

    def __reindex_similarity_matrix(self, similarity_matrix):
        return similarity_matrix.reindex(index=self.species_order, columns=self.species_order, copy=False)

    def calculate_weighted_similarities(self, relative_abundances):
        return self.similarity_matrix.to_numpy() @ relative_abundances


class Metacommunity:
    """Class for metacommunities and calculating their diversity.

    All diversities computed by objects of this class are
    similarity-sensitive. See https://arxiv.org/abs/1404.6520 for
    precise definitions of the various diversity measures.
    """

    def __init__(self, similarity, abundance):
        """Initializes object.

        Parameters
        ----------
        similarity: diversity.metacommunity.Similarity
            Object for calculating abundance-weighted similarities.
        abundance: diversity.metacommunity.Abundance
            Object whose (sub-/meta-)community species abundances are
            used.
        """
        self.similarity = similarity
        self.abundance = abundance

    @cached_property
    def metacommunity_similarity(self):
        """Sums of similarities weighted by metacommunity abundances."""
        return self.similarity.calculate_weighted_similarities(
            self.abundance.metacommunity_abundance
        )

    @cached_property
    def subcommunity_similarity(self):
        """Sums of similarities weighted by subcommunity abundances."""
        return self.similarity.calculate_weighted_similarities(
            self.abundance.subcommunity_abundance
        )

    @cached_property
    def normalized_subcommunity_similarity(self):
        """Sums of similarities weighted by the normalized subcommunity abundances."""
        return self.similarity.calculate_weighted_similarities(
            self.abundance.normalized_subcommunity_abundance
        )

    def subcommunity_alpha(self, viewpoint):
        """Calculates alpha class diversities of subcommunities.

        Corresponds roughly to the diversities of subcommunities
        relative to the metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__subcommunity_measure(viewpoint, 1, self.subcommunity_similarity)

    def subcommunity_rho(self, viewpoint):
        """Calculates rho class diversities of subcommunities.

        Corresponds roughly to how redundant each subcommunity's classes
        are in the metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__subcommunity_measure(
            viewpoint,
            self.metacommunity_similarity,
            self.subcommunity_similarity,
        )

    def subcommunity_beta(self, viewpoint):
        """Calculates beta class diversities of subcommunities.

        Corresponds roughly to how distinct each subcommunity's classes
        are from all classes in metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only
            the most frequent species.
        """
        return 1 / self.subcommunity_rho(viewpoint)

    def subcommunity_gamma(self, viewpoint):
        """Calculates gamma class diversities of subcommunities.

        Corresponds roughly to how much each subcommunity contributes
        towards the metacommunity diversity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only
            the most frequent species.
        """
        denominator = broadcast_to(
            self.metacommunity_similarity,
            self.abundance.normalized_subcommunity_abundance.shape,
        )
        return self.__subcommunity_measure(viewpoint, 1, denominator)

    def normalized_subcommunity_alpha(self, viewpoint):
        """Calculates normalized alpha class diversities of subcommunities.

        Corresponds roughly to the diversities of subcommunities in
        isolation.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__subcommunity_measure(
            viewpoint, 1, self.normalized_subcommunity_similarity
        )

    def normalized_subcommunity_rho(self, viewpoint):
        """Calculates normalized rho class diversities of subcommunities.

        Corresponds roughly to the representativeness of subcommunities.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__subcommunity_measure(
            viewpoint,
            self.metacommunity_similarity,
            self.normalized_subcommunity_similarity,
        )

    def normalized_subcommunity_beta(self, viewpoint):
        """Calculates normalized rho class diversities of subcommunities.

        Corresponds roughly to average diversity of subcommunities in
        the metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return 1 / self.normalized_subcommunity_rho(viewpoint)

    def metacommunity_alpha(self, viewpoint):
        """Calculates alpha class diversity of metacommunity.

        Corresponds roughly to the average diversity of subcommunities
        relative to the metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__metacommunity_measure(viewpoint, self.subcommunity_alpha)

    def metacommunity_rho(self, viewpoint):
        """Calculates rho class diversitiy of metacommunity.

        Corresponds roughly to the average redundancy of subcommunities
        in the metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__metacommunity_measure(viewpoint, self.subcommunity_rho)

    def metacommunity_beta(self, viewpoint):
        """Calculates beta class diversity of metacommunity.

        Corresponds roughly to the average distinctness of
        subcommunities within the metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__metacommunity_measure(viewpoint, self.subcommunity_beta)

    def metacommunity_gamma(self, viewpoint):
        """Calculates gamma class diversity of metacommunity.

        Corresponds roughly to the class diversity of the unpartitioned
        metacommunity.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__metacommunity_measure(viewpoint, self.subcommunity_gamma)

    def normalized_metacommunity_alpha(self, viewpoint):
        """Calculates alpha class diversity of metacommunity.

        Corresponds roughly to the average diversity of subcommunities
        in isolation.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__metacommunity_measure(
            viewpoint, self.normalized_subcommunity_alpha
        )

    def normalized_metacommunity_rho(self, viewpoint):
        """Calculates rho class diversitiy of metacommunity.

        Corresponds roughly to the average representativeness of
        subcommunities.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__metacommunity_measure(viewpoint, self.normalized_subcommunity_rho)

    def normalized_metacommunity_beta(self, viewpoint):
        """Calculates beta class diversity of metacommunity.

        Corresponds roughly to the effective number of distinct
        subcommunities.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return self.__metacommunity_measure(
            viewpoint, self.normalized_subcommunity_beta
        )

    def __subcommunity_measure(self, viewpoint, numerator, denominator):
        """Calculates subcommunity diversity measures."""
        similarities = divide(
            numerator, denominator, out=zeros(denominator.shape), where=denominator != 0
        )
        return power_mean(
            1 - viewpoint,
            self.abundance.normalized_subcommunity_abundance,
            similarities,
        )

    def __metacommunity_measure(self, viewpoint, subcommunity_function):
        """Calculates metcommunity diversity measures."""
        subcommunity_measure = subcommunity_function(viewpoint)
        return power_mean(
            1 - viewpoint,
            self.abundance.subcommunity_normalizing_constants,
            subcommunity_measure,
        )

    def subcommunities_to_dataframe(self, viewpoint):
        """Table containing all subcommunity diversity values.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species.
        """
        return DataFrame(
            {
                "community": self.abundance.subcommunity_order,
                "viewpoint": viewpoint,
                "alpha": self.subcommunity_alpha(viewpoint),
                "rho": self.subcommunity_rho(viewpoint),
                "beta": self.subcommunity_beta(viewpoint),
                "gamma": self.subcommunity_gamma(viewpoint),
                "normalized_alpha": self.normalized_subcommunity_alpha(viewpoint),
                "normalized_rho": self.normalized_subcommunity_rho(viewpoint),
                "normalized_beta": self.normalized_subcommunity_beta(viewpoint),
            }
        )

    def metacommunity_to_dataframe(self, viewpoint):
        """Table containing all metacommunity diversity values.

        Parameters
        ----------
        viewpoint: numeric
            Non-negative number. Can be interpreted as the degree of
            ignorance towards rare species, where 0 treats rare species
            the same as frequent species, and infinity considers only the
            most frequent species."""
        return DataFrame(
            {
                "community": "metacommunity",
                "viewpoint": viewpoint,
                "alpha": self.metacommunity_alpha(viewpoint),
                "rho": self.metacommunity_rho(viewpoint),
                "beta": self.metacommunity_beta(viewpoint),
                "gamma": self.metacommunity_gamma(viewpoint),
                "normalized_alpha": self.normalized_metacommunity_alpha(viewpoint),
                "normalized_rho": self.normalized_metacommunity_rho(viewpoint),
                "normalized_beta": self.normalized_metacommunity_beta(viewpoint),
            },
            index=[0],
        )


def make_similarity(similarity_matrix, species_subset, chunk_size):
    similarity_type = type(similarity_matrix)
    if similarity_type not in {DataFrame, str}:
        raise InvalidArgumentError(
            "similarity_matrix must be a str or a pandas.DataFrame, but"
            f"was: {similarity_type}."
        )
    similarity_classes = {
        DataFrame: SimilarityFromMemory, 
        str: SimilarityFromFile
    }
    similarity_arguments = {
        DataFrame: (similarity_matrix, species_subset),
        str: (similarity_matrix, species_subset, chunk_size),
    }
    similarity_class = similarity_classes[similarity_type]
    initializer_arguments = similarity_arguments[similarity_type]
    return similarity_class(*initializer_arguments)


def subset_subcommunities(counts, subcommunities, subcommunity_column):
    if subcommunities is None:
        return counts
    return counts[counts[subcommunity_column].isin(subcommunities)]


def make_metacommunity(
    counts,
    similarity_matrix,
    subcommunities=None,
    chunk_size=1,
    subcommunity_column="subcommunity",
    species_column="species",
    count_column="count",
):
    """Builds a Metacommunity object from specified parameters.

    Parameters
    ----------
    counts: numpy.ndarray or pandas.DataFrame
        See diversity.metacommunity.Abundance. If the object is a
        pandas.DataFrame, its to_numpy method should return the expected
        numpy.ndarray.
    similarity_matrix: pandas.DataFrame, or str
        For data frame, see diversity.metacommunity.SimilarityFromMemory,
        and for str, see diversity.metacommunity.SimilarityFromFile.
    subcommunities: Set
        Names of subcommunities to include. Their union is the
        metacommunity, and data for all other subcommunities is ignored.
    chunk_size: int
        Optional. See diversity.metacommunity.SimilarityFromFile.
    subcommunity_column, species_column, count_column: str
        Used to specify non-default column headers. See
        diversity.metacommunity.Abundance.

    Returns
    -------
    A diversity.metacommunity.Metacommunity object built according to
    parameter specification.
    """
    LOGGER.debug(
        "make_metacommunity(%s, %s, subcommunities=%s, chunk_size=%s,"
        " subcommunity_column=%s, species_column=%s, count_column=%s"
        % (
            counts,
            similarity_matrix,
            subcommunities,
            chunk_size,
            subcommunity_column,
            species_column,
            count_column,
        )
    )

    counts_subset = subset_subcommunities(counts, subcommunities, subcommunity_column)
    species_subset = unique(counts_subset[species_column])
    similarity = make_similarity(similarity_matrix, species_subset, chunk_size)
    abundance = Abundance(
        counts_subset,
        similarity.species_order,
        subcommunity_column=subcommunity_column,
        species_column=species_column,
        count_column=count_column,
    )
    return Metacommunity(similarity, abundance)


def make_pairwise_metacommunities(counts, similarity_matrix, subcommunity_column, **kwargs):
    subcommunties_groups = counts.groupby(subcommunity_column)
    pairwise_metacommunities = []
    for i, (_, group_i) in enumerate(subcommunties_groups):
            for j, (_, group_j) in enumerate(subcommunties_groups):
                if j > i:
                    counts = concat([group_i, group_j])
                    pair_ij = make_metacommunity(
                        counts, 
                        similarity_matrix, 
                        subcommunity_column=subcommunity_column, 
                        **kwargs)
                    pairwise_metacommunities.append(pair_ij)
    return pairwise_metacommunities