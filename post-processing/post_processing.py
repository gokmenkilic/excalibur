import argparse
import operator as op
import traceback
from functools import reduce
from pathlib import Path

import pandas as pd
from config_handler import ConfigHandler
from perflog_handler import PerflogHandler
from plot_handler import plot_generic


class PostProcessing:

    def __init__(self, debug=False, verbose=False):
        # FIXME: add proper logging
        self.debug = debug
        self.verbose = verbose
        # FIXME: add df and config directly to init?

    def run_post_processing(self, log_path, config: ConfigHandler):
        """
            Return a dataframe containing the information passed to a plotting script
            and produce relevant graphs.

            Args:
                log_path: path, path to a log file or a directory containing log files.
                config: ConfigHandler, class containing configuration information for plotting.
        """

        # find and read perflogs
        self.df = PerflogHandler(log_path, self.debug).read_all_perflogs()

        # apply column types
        self.apply_df_types(config.all_columns, config.column_types)
        # sort rows
        # NOTE: sorting here is necessary to ensure correct filtering + scaling alignment
        self.sort_df(config.x_axis, config.series_columns)
        # filter data
        mask = self.filter_df(config.and_filters, config.or_filters, config.series_filters)
        self.check_filtered_row_count(mask, config.x_axis, config.series_columns, config.plot_columns)
        # scale y-axis
        self.transform_df_data(config.x_axis, config.y_axis, config.series_filters, mask)

        # FIXME: have an option to put this into a file (-s / --save flag?)
        print("Selected dataframe:")
        print(self.df[config.plot_columns][mask])

        # call a plotting script
        plot_generic(
            config.title, self.df[config.plot_columns][mask],
            config.x_axis, config.y_axis, config.series_filters, self.debug)

        # FIXME: maybe save this bit to a file as well for easier viewing
        if self.debug & self.verbose:
            print("")
            print("Full dataframe:")
            print(self.df.to_json(orient="columns", indent=2))

        return self.df[config.plot_columns][mask]

    def check_df_columns(self, all_columns):
        """
            Check that all columns listed in the config exist in the dataframe.

            Args:
                all_columns: list, names of all columns mentioned in the config.
        """

        invalid_columns = []
        # check for invalid columns
        for col in all_columns:
            if col not in self.df.columns:
                invalid_columns.append(col)
        if invalid_columns:
            raise KeyError("Could not find columns", invalid_columns)

    def apply_df_types(self, all_columns, column_types):
        """
            Apply user-specified types to all relevant columns in the dataframe.

            Args:
                all_columns: list, names of all columns mentioned in the config.
                column_types: dict, name-type pairs for all important columns.
        """

        # validate columns
        self.check_df_columns(all_columns)

        for col in all_columns:
            if column_types.get(col):

                # get user input type
                conversion_type = column_types[col]
                # allow user to specify "datetime" as a type (internally convert to "datetime64")
                conversion_type += "64" if conversion_type == "datetime" else ""

                # internal type conversion
                if pd.api.types.is_string_dtype(conversion_type):
                    # all strings treated as object (nullable)
                    conversion_type = "object"
                elif pd.api.types.is_float_dtype(conversion_type):
                    # all floats treated as float64 (nullable)
                    conversion_type = "float64"
                elif pd.api.types.is_integer_dtype(conversion_type):
                    # all integers treated as Int64 (nullable)
                    # NOTE: default pandas integer type is int64 (not nullable)
                    conversion_type = "Int64"
                elif pd.api.types.is_datetime64_any_dtype(conversion_type):
                    # all datetimes treated as datetime64[ns] (nullable)
                    conversion_type = "datetime64[ns]"
                else:
                    raise RuntimeError("Unsupported user-specified type '{0}' for column '{1}'."
                                       .format(conversion_type, col))

                # skip type conversion if column is already the desired type
                if conversion_type == self.df[col].dtype:
                    continue
                # otherwise apply type to column
                self.df[col] = self.df[col].astype(conversion_type)

            else:
                raise KeyError("Could not find user-specified type for column", col)

    def sort_df(self, x_axis, series_columns):
        """
            Sort the given dataframe such that x-axis values and series are in ascending order.

            Args:
                x_axis: dict, x-axis column and units.
                series_columns: list, names of series columns.
        """

        sorting_columns = [x_axis["value"]]
        if series_columns:
            # NOTE: currently assuming there can only be one unique series column
            sorting_columns.append(series_columns[0])
        self.df.sort_values(sorting_columns, inplace=True, ignore_index=True)

    def filter_df(self, and_filters, or_filters, series_filters):
        """
            Return a mask for the given dataframe based on user-specified filter conditions.

            Args:
                and_filters: list, filter conditions to be concatenated together with logical AND.
                or_filters: list, filter conditions to be concatenated together with logical OR.
                series_filters: list, function like or_filters but use series to select x-axis groups.
        """

        mask = pd.Series(self.df.index.notnull())
        # filter rows
        if and_filters:
            mask = reduce(op.and_, (self.row_filter(f, self.df) for f in and_filters))
        if or_filters:
            mask &= reduce(op.or_, (self.row_filter(f, self.df) for f in or_filters))
        # apply series filters
        if series_filters:
            mask &= reduce(op.or_, (self.row_filter(f, self.df) for f in series_filters))
        # ensure not all rows are filtered away
        if self.df[mask].empty:
            raise pd.errors.EmptyDataError("Filtered dataframe is empty", self.df[mask].index)

        return mask

    def check_filtered_row_count(self, mask, x_axis, series_columns, plot_columns):
        """
            Check that the filtered dataframe does not have an incompatible number of rows.
            Row number must match number of unique x-axis values per series.

            Args:
                mask: bool series, dataframe filters.
                x_axis: dict, x-axis column and units.
                series_columns: list, names of series columns.
                plot_columns: list, names of all columns needed for plotting.
        """

        # get number of occurrences of each column
        series_col_count = {c: series_columns.count(c) for c in series_columns}
        # get number of column combinations
        series_combinations = reduce(op.mul, list(series_col_count.values()), 1)
        num_filtered_rows = len(self.df[mask])
        num_x_data_points = series_combinations * len(set(self.df[mask][x_axis["value"]]))
        # check expected number of rows
        if num_filtered_rows > num_x_data_points:
            raise RuntimeError("Unexpected number of rows ({0}) does not match \
                               number of unique x-axis values per series ({1})"
                               .format(num_filtered_rows, num_x_data_points),
                               self.df[plot_columns][mask])

    def transform_df_data(self, x_axis, y_axis, series_filters, mask):
        """
            Transform dataframe y-axis based on scaling settings.

            Args:
                x_axis: dict, x-axis column and units.
                y_axis: dict, y-axis column, units, and scaling information.
                series_filters: list, x-axis group filters.
                mask: bool series, dataframe filters.
        """

        # FIXME: overhaul this section
        scaling_column = None
        scaling_series_mask = None
        scaling_x_value_mask = None
        # extract scaling information
        if y_axis.get("scaling"):

            # check column information
            if y_axis["scaling"].get("column"):
                # copy scaling column (prevents issues when scaling by itself)
                scaling_column = self.df[y_axis["scaling"]["column"]["name"]].copy()
                # get mask of scaling series
                if y_axis["scaling"]["column"].get("series") is not None:
                    scaling_series_mask = self.row_filter(
                        series_filters[y_axis["scaling"]["column"]["series"]], self.df)
                # get mask of scaling x-value
                if y_axis["scaling"]["column"].get("x_value"):
                    scaling_x_value_mask = (
                        self.df[x_axis["value"]] == y_axis["scaling"]["column"]["x_value"])

            # check custom value is not zero
            elif not y_axis["scaling"].get("custom"):
                raise RuntimeError("Invalid custom scaling value (cannot divide by {0})."
                                   .format(y_axis["scaling"].get("custom")))

            # apply data transformation per series
            if series_filters:
                for f in series_filters:
                    m = self.row_filter(f, self.df)
                    self.transform_axis(
                        mask & m, y_axis, scaling_column,
                        scaling_series_mask, scaling_x_value_mask)
            # apply data transformation to all data
            else:
                self.transform_axis(
                    mask, y_axis, scaling_column,
                    scaling_series_mask, scaling_x_value_mask)

        # FIXME: add this as a config option at some point
        # if y_axis.get("drop_nan"):
        #    df.dropna(subset=[y_axis["value"]], inplace=True)
            # reset index
        #    df.index = range(len(df.index))

    # operator lookup dictionary
    op_lookup = {
        "==":   op.eq,
        "!=":   op.ne,
        "<":    op.lt,
        ">":    op.gt,
        "<=":   op.le,
        ">=":   op.ge
    }

    def row_filter(self, filter, df: pd.DataFrame):
        """
            Return a dataframe mask based on a filter condition. The filter is a list that
            contains a column name, an operator, and a value (e.g. ["flops_value", ">=", 1.0]).

            Args:
                filter: list, a condition based on which a dataframe is filtered.
                df: dataframe, used to create a mask by having the filter condition applied to it.
        """

        column, str_op, value = filter
        if self.debug:
            print("Applying row filter condition:", column, str_op, value)

        # check operator validity
        operator = self.op_lookup.get(str_op)
        if operator is None:
            raise KeyError("Unknown comparison operator", str_op)

        # evaluate expression and extract dataframe mask
        if value is None:
            mask = df[column].isnull() if operator == op.eq else df[column].notnull()
        else:
            try:
                # interpret comparison value as column dtype
                value = pd.Series(value, dtype=df[column].dtype).iloc[0]
                mask = operator(df[column], value)
            except TypeError or ValueError as e:
                e.args = (e.args[0] + " for column '{0}' and value '{1}'".format(column, value),)
                raise

        if self.debug & self.verbose:
            print(mask)
        if self.debug:
            print("")

        return mask

    def transform_axis(self, mask, axis, scaling_column, scaling_series_mask, scaling_x_value_mask):
        """
            Divide axis values by specified values and reflect this change in the dataframe.

            Args:
                mask: bool series, dataframe filters.
                axis: dict, axis column, units, and values to scale by.
                scaling_column: dataframe column, copy of column containing values to scale by.
                scaling_series_mask: bool series, a series mask to be applied to the scaling column.
                scaling_x_value_mask: bool series, an x-axis value mask to be applied to the scaling column.
        """

        # scale by column
        if scaling_column is not None:

            # check types
            if (not pd.api.types.is_numeric_dtype(self.df[mask][axis["value"]].dtype) or
                not pd.api.types.is_numeric_dtype(scaling_column.dtype)):
                # both columns must be numeric
                raise TypeError("Cannot scale column '{0}' of type {1} by column '{2}' of type {3}."
                                .format(axis["value"], self.df[mask][axis["value"]].dtype,
                                        axis["scaling"]["column"]["name"], scaling_column.dtype))

            # get mask of scaling value(s)
            scaling_mask = mask.copy()
            if scaling_series_mask is not None:
                scaling_mask = scaling_series_mask
            if scaling_x_value_mask is not None:
                scaling_mask &= scaling_x_value_mask

            scaling_val = (scaling_column[scaling_mask].iloc[0]
                           if len(scaling_column[scaling_mask]) == 1
                           else scaling_column[scaling_mask].values)

            # FIXME: add a check that the masked scaling column has the same number of values
            # as the masked df (unless there is only one scaling value)

            self.df.loc[mask, axis["value"]] = self.df[mask][axis["value"]].values / scaling_val
            # FIXME: add this as a config option at some point in conjunction with dropping NaNs
            # df[axis["value"]].replace(to_replace=1, value=np.NaN, inplace=True)

        # scale by custom value
        elif axis["scaling"].get("custom"):
            scaling_value = axis["scaling"]["custom"]
            try:
                # interpret scaling value as column dtype
                scaling_value = pd.Series(scaling_value, dtype=self.df[mask][axis["value"]].dtype).iloc[0]
            except ValueError as e:
                e.args = (e.args[0] + " as a scaling value for column '{0}'".format(axis["value"]),)
                raise
            self.df.loc[mask, axis["value"]] /= scaling_value


def read_args():
    """
        Return parsed command line arguments.
    """

    parser = argparse.ArgumentParser(
        description="Plot benchmark data. At least one perflog must be supplied.")

    # required positional arguments (log path, config path)
    parser.add_argument("log_path", type=Path,
                        help="path to a perflog file or a directory containing perflog files")
    parser.add_argument("config_path", type=Path,
                        help="path to a configuration file specifying what to plot")

    # optional argument (plot type)
    parser.add_argument("-p", "--plot_type", type=str, default="generic",
                        help="type of plot to be generated (default: 'generic')")

    # info dump flags
    parser.add_argument("-d", "--debug", action="store_true",
                        help="debug flag for printing additional information")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="verbose flag for printing more debug information \
                              (must be used in conjunction with the debug flag)")

    return parser.parse_args()


def main():

    args = read_args()
    post = PostProcessing(args.debug, args.verbose)

    try:
        config = ConfigHandler.from_path(args.config_path)
        post.run_post_processing(args.log_path, config)

    except Exception as e:
        print(type(e).__name__ + ":", e)
        print("Post-processing stopped")
        if args.debug:
            print(traceback.format_exc())


if __name__ == "__main__":
    main()
