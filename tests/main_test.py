"""Tests for diversity.diversity."""
from pytest import fixture
from pandas import read_csv, DataFrame
from pandas.testing import assert_frame_equal

from diversity.metacommunity import Abundance, Metacommunity, Similarity

TEST_VIEWPOINT = 0
TEST_DF = DataFrame(
    {
        "community": ["subcommunity_1", "subcommunity_2"],
        "viewpoint": [0, 0],
        "alpha": [3.0, 3.0],
        "rho": [2.05, 2.05],
        "beta": [0.487805, 0.487805],
        "gamma": [1.463415, 1.463415],
        "normalized_alpha": [1.5, 1.5],
        "normalized_rho": [1.025, 1.025],
        "normalized_beta": [0.97561, 0.97561],
    }
)


@fixture
def mock_input_file(tmp_path):
    mock_csv_data = [
        "subcommunity\tspecies\tcount",
        "subcommunity_1\tspecies_1\t1",
        "subcommunity_1\tspecies_2\t1",
        "subcommunity_1\tspecies_3\t1",
        "subcommunity_2\tspecies_4\t1",
        "subcommunity_2\tspecies_5\t1",
        "subcommunity_2\tspecies_6\t1",
    ]
    datafile = tmp_path / "input.csv"
    datafile.write_text("\n".join(mock_csv_data))
    return str(datafile)


@fixture
def mock_matrix_file(tmp_path):
    mock_csv_data = [
        "species_1\tspecies_2\tspecies_3\tspecies_4\tspecies_5\tspecies_6",
        "1.0\t0.5\t0.5\t0.7\t0.7\t0.7",
        "0.5\t1.0\t0.5\t0.7\t0.7\t0.7",
        "0.5\t0.5\t1.0\t0.7\t0.7\t0.7",
        "0.7\t0.7\t0.7\t1.0\t0.5\t0.5",
        "0.7\t0.7\t0.7\t0.5\t1.0\t0.5",
        "0.7\t0.7\t0.7\t0.5\t0.5\t1.0",
    ]
    datafile = tmp_path / "matrix.csv"
    datafile.write_text("\n".join(mock_csv_data))
    return str(datafile)


@fixture
def mock_empty_matrix_file(tmp_path):
    datafile = tmp_path / "empty_matrix.csv"
    return str(datafile)


def test_subcommunity_from_file(mock_input_file, mock_matrix_file):
    df = read_csv(mock_input_file, sep="\t")
    similarity = Similarity(similarities_filepath=mock_matrix_file)
    abundance = Abundance(counts=df.to_numpy(), species_order=similarity.species_order)
    meta = Metacommunity(similarity=similarity, abundance=abundance)
    output_df = meta.subcommunities_to_dataframe(viewpoint=TEST_VIEWPOINT)
    assert_frame_equal(TEST_DF, output_df, check_exact=False)
