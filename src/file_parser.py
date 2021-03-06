import datetime
import os
import pandas as pd
import re
import shutil
from fsplit.filesplit import Filesplit
from typing import Callable, Dict, List, Tuple, Type

from src.log_parsers import UnparsableLogError, LogParser
from src.parallel_executor import ParallelExecutor, params
from src.utils import Timer, df2tsv


FILE_CHUNK_SORT_MASK = re.compile(r'^chunk_(?P<id>\d+)(?:[.].*)?$')


class FileParser(ParallelExecutor):
    """Main log file parser class."""
    unparsed_short_name = 'na'
    records_ext = '.records'
    keys_ext = '.keys'

    def __init__(self,
                 log_parsers: List[Type[LogParser]],
                 max_processes: int = None,
                 max_threads: int = None,
                 parse_chunk_size: int = 1_000_000_000,  # ~1GB
                 delete_intermediate_result_dirs: bool = True,
                 df_export_func: Callable = df2tsv):
        super().__init__(max_processes=max_processes,
                         max_threads=max_threads,
                         auto_log_msg_prefix="(parallel executor) ")
        self.log_parsers = log_parsers
        self.chunk_byte_size = parse_chunk_size
        self.delete_intermediate_result_dirs = delete_intermediate_result_dirs
        self.export_df = df_export_func

    def parse_file(self, src_file_path: str, out_dir_path: str = None) -> None:
        assert os.path.exists(src_file_path), 'Specified source file path does not exist'
        if out_dir_path:
            assert not os.path.exists(out_dir_path), 'Specified output directory already exists'
        self.log.info(f'Parsing: {src_file_path}')
        try:
            with Timer() as timer:
                self._parse_file_main(src_file_path, out_dir_path)
        except Exception as e:
            self.log.critical(f'Parsing failed with exception: {str(e)}', exc_info=True)
        else:
            self.log.info(f'Parsing completed (wall time: {timer.time_string})')

    def _parse_file_main(self, logs_file_path: str, out_dir_path: str = None) -> None:
        # define main output directory
        logs_file_dir, logs_file_name_base, _ = self.split_file_path(logs_file_path)
        run_timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        output_dir_path = out_dir_path or os.path.join(logs_file_dir, f'{logs_file_name_base}_{run_timestamp}')

        # define temporary output subdirectories
        split_dir_path = os.path.join(output_dir_path, '.0_split')
        parsed_dir_path = os.path.join(output_dir_path, '.1_parsed')
        tabularized_dir_path = os.path.join(output_dir_path, '.2_tabularized')

        # initialize main output directory
        self.log.info(f'Initializing output directory: {output_dir_path}')
        os.makedirs(output_dir_path)

        # split source file into evenly sized chunks of logs
        self.log.info('STAGE_1: Splitting source file into chunks...')
        self._create_temp_directory(split_dir_path)
        self._split_file_into_chunks(src_file_path=logs_file_path,
                                     dst_dir_path=split_dir_path)
        self.log.info(f'STAGE_1: Source file split into chunks')

        # extract features from chunks of logs and save them as records
        self.log.info('STAGE_2: Parsing source file chunks...')
        self._create_temp_directory(parsed_dir_path)
        self._parse_file_chunks(src_dir_path=split_dir_path,
                                dst_dir_path=parsed_dir_path)
        self._remove_temp_directory(split_dir_path)
        self.log.info('STAGE_2: Done parsing source file chunks')

        # get all unique feature names extracted from chunks by each parser and order them to form column names
        self.log.info('STAGE_3: Gathering unique feature names...')
        records_table_headers_dict = self._get_final_table_headers(src_dir_path=parsed_dir_path)
        self.log.info('STAGE_3: Done gathering unique feature names')

        # convert files with records into tabularic tsv files with matching headers
        self.log.info('STAGE_4: Tabularizing parsed file chunks...')
        self._create_temp_directory(tabularized_dir_path)
        self._tabularize_parsed_chunks(src_dir_path=parsed_dir_path,
                                       dst_dir_path=tabularized_dir_path,
                                       records_table_headers_dict=records_table_headers_dict)
        self.log.info('STAGE_4: Done tabularizing parsed file chunks')

        # merge files with leftover logs that were not parsed by any of the parsers
        self.log.info('STAGE_5: Merging unparsed file chunks...')
        self._concatenate_unparsed_chunks(src_dir_path=parsed_dir_path,
                                          dst_dir_path=output_dir_path,
                                          orig_file_name_base=logs_file_name_base)
        self._remove_temp_directory(parsed_dir_path)
        self.log.info('STAGE_5: Done merging unparsed file chunks')

        # merge parsed table chunks
        self.log.info('STAGE_6: Merging parsed file chunks...')
        self._concatenate_tabularized_chunks(src_dir_path=tabularized_dir_path,
                                             dst_dir_path=output_dir_path,
                                             orig_file_name_base=logs_file_name_base)
        self._remove_temp_directory(tabularized_dir_path)
        self.log.info('STAGE_6: Done merging parsed file chunks')

    def _split_file_into_chunks(self, src_file_path: str, dst_dir_path: str) -> None:
        # define helper method that renames file chunks created with FileSplit
        def rename_chunk(chunk_path: str):
            directory, name, extension = self.split_file_path(chunk_path)
            chunk_id = re.match(r'^.*_(?P<id>\d+)$', name).group('id')
            new_chunk_path = os.path.join(directory, f'chunk_{chunk_id}{extension}')
            os.rename(chunk_path, new_chunk_path)
            return new_chunk_path

        # define callback function for FileSplit.split
        def note_created_file(file_path: str, file_path_list: list):
            self.log.debug(f'Creating file: {file_path}')
            file_path_list.append(file_path)

        # if source file size is bigger than defined chunk size split it into chunks
        if os.path.getsize(src_file_path) > self.chunk_byte_size:
            # initialize file paths list
            chunk_file_paths = list()

            # split file
            fs = Filesplit()
            fs.split(file=src_file_path,
                     split_size=self.chunk_byte_size,
                     output_dir=dst_dir_path,
                     callback=lambda path, _: note_created_file(path, chunk_file_paths),
                     newline=True)

            # rename files on file paths list
            chunk_file_paths = [rename_chunk(path) for path in chunk_file_paths]

            # delete manifest file
            manifest_file_path = os.path.join(dst_dir_path, 'fs_manifest.csv')
            if os.path.exists(manifest_file_path):
                os.remove(manifest_file_path)

        else:
            # copy and rename the source file
            _, _, extension = self.split_file_path(src_file_path)
            dst_file_path = os.path.join(dst_dir_path, f'chunk_1{extension}')
            shutil.copy(src_file_path, dst_file_path)

    def _parse_file_chunks(self,
                           src_dir_path: str,
                           dst_dir_path: str
                           ) -> None:

        # get chunk file paths
        chunk_file_names = self.get_sorted_chunk_names(src_dir_path=src_dir_path)
        chunk_file_paths = [os.path.join(src_dir_path, n) for n in chunk_file_names]

        # parse chunk files (concurrently)
        params_list = [params(src_file_path, dst_dir_path) for src_file_path in chunk_file_paths]
        self.execute_parallel_task(task=self._parse_file_chunk,
                                   params_list=params_list)

    def _parse_file_chunk(self,
                          src_file_path: str,
                          dst_dir_path: str
                          ) -> None:
        # parse file path
        _, src_file_name, src_file_ext = self.split_file_path(src_file_path)

        # initialize log parsers
        parsers = {p.short_name: p() for p in self.log_parsers}

        # initialize log parsing results
        records_dict = {k: [] for k in parsers.keys()}
        keys_dict = {k: set() for k in parsers.keys()}
        unparsed_logs = []

        # parse logs file chunk
        with open(src_file_path) as file:
            for log_entry in file:
                for parser_name, parser in parsers.items():
                    try:
                        record = parser.parse(log_entry)
                        records_dict[parser_name].append(record)
                        keys_dict[parser_name].update(record)
                        break
                    except UnparsableLogError:
                        pass
                else:
                    unparsed_logs.append(log_entry.strip())

        # save records and keys of parsed logs
        self._persist_parsed_data(src_file_name=src_file_name,
                                  dst_dir_path=dst_dir_path,
                                  records_dict=records_dict,
                                  keys_dict=keys_dict)

        # save unparsed logs
        self._persist_unparsed_logs(src_file_name=src_file_name,
                                    dst_dir_path=dst_dir_path,
                                    file_ext=src_file_ext,
                                    unparsed_logs=unparsed_logs)

    def _persist_parsed_data(self, src_file_name: str, dst_dir_path: str, records_dict: dict, keys_dict: dict) -> None:
        # create output directories (if they don't already exist)
        for parser_name in records_dict.keys():
            self._create_temp_directory(os.path.join(dst_dir_path, parser_name), exist_ok=True)

        # save successfully parsed results as records and keys (unique features from all records)
        for parser_name, records in records_dict.items():
            records_file_name = f'{src_file_name}.{parser_name}{self.records_ext}'
            records_file_path = os.path.join(dst_dir_path, parser_name, records_file_name)

            self.log.debug(f'Creating file: {records_file_path}')
            with open(records_file_path, mode='w+') as file:
                for record in records:
                    file.write(str(record) + '\n')

        for parser_name, keys in keys_dict.items():
            keys_file_name = f'{src_file_name}.{parser_name}{self.keys_ext}'
            keys_file_path = os.path.join(dst_dir_path, parser_name, keys_file_name)

            self.log.debug(f'Creating file: {keys_file_path}')
            with open(keys_file_path, mode='w+') as file:
                for key in keys:
                    file.write(str(key) + '\n')

    def _persist_unparsed_logs(self, src_file_name: str, dst_dir_path: str, file_ext: str, unparsed_logs: list) -> None:
        # create output directory
        self._create_temp_directory(os.path.join(dst_dir_path, self.unparsed_short_name), exist_ok=True)

        # save unparsed logs
        unparsed_file_name = f'{src_file_name}.{self.unparsed_short_name}{file_ext}'
        unparsed_file_path = os.path.join(dst_dir_path, self.unparsed_short_name, unparsed_file_name)

        with open(unparsed_file_path, mode='w+') as file:
            self.log.debug(f'Creating file: {unparsed_file_path}')
            for log in unparsed_logs:
                file.write(str(log) + '\n')

    def _get_final_table_headers(self, src_dir_path: str) -> Dict[str, List[str]]:
        headers_dict = dict()
        for parser in self.log_parsers:
            # get directory with parser's output
            parser_name = str(parser.short_name)
            parser_results_dir = os.path.join(src_dir_path, parser_name)

            # get chunk .keys file paths
            chunk_keys_file_names = self.get_sorted_chunk_names(src_dir_path=parser_results_dir,
                                                                mask=rf'^.*{self.keys_ext}$')
            chunk_keys_file_paths = [os.path.join(src_dir_path, parser_name, n) for n in chunk_keys_file_names]

            # get unique keys sets for each parser
            unique_keys = set()
            for file_path in chunk_keys_file_paths:
                with open(file_path) as file:
                    lines = file.read()
                keys = filter(None, lines.split('\n'))
                unique_keys.update(keys)

            # sort keys so that user made fields (starting with '_') are a the beginning
            sorted_keys = sorted(unique_keys,
                                 key=lambda x: '0' + str(x).lower() if str(x).startswith('_') else '1' + str(x).lower())
            headers_dict[parser_name] = sorted_keys

        return headers_dict

    def _tabularize_parsed_chunks(self,
                                  src_dir_path: str,
                                  dst_dir_path: str,
                                  records_table_headers_dict: Dict[str, List[str]],
                                  ) -> None:
        # get params for tabularizer function
        params_list = []
        for parser in self.log_parsers:
            # get directory with parser's output
            parser_name = str(parser.short_name)
            parser_results_dir = os.path.join(src_dir_path, parser_name)

            # get chunk .records file paths
            chunk_records_file_names = self.get_sorted_chunk_names(src_dir_path=parser_results_dir,
                                                                   mask=rf'^.*{self.records_ext}$')
            chunk_records_file_paths = [os.path.join(src_dir_path, parser_name, n) for n in chunk_records_file_names]

            # one create a param set for each file path
            for chunk_records_file_path in chunk_records_file_paths:
                params_list.append(
                    params(
                        parser_name=parser_name,
                        src_file_path=chunk_records_file_path,
                        dst_dir_path=dst_dir_path,
                        table_headers=records_table_headers_dict[parser_name]
                    )
                )

        # convert record files into tables (tsv) with specified headers
        self.execute_parallel_task(task=self._tabularize_parsed_chunk,
                                   params_list=params_list)

    def _tabularize_parsed_chunk(self,
                                 parser_name: str,
                                 src_file_path: str,
                                 dst_dir_path: str,
                                 table_headers: List[str]
                                 ) -> None:
        # parse source file path
        src_file_dir, src_file_name, src_file_ext = self.split_file_path(src_file_path)

        # get result file path
        dst_file_name = f'{src_file_name}.tsv'
        dst_file_path = os.path.join(dst_dir_path, parser_name, dst_file_name)

        # load records from file
        with open(src_file_path) as file:
            lines = file.readlines()
        records = list(map(eval, lines))

        # format records as table
        result_df = pd.DataFrame(columns=table_headers)
        result_df = result_df.append(records, ignore_index=True)

        # export table to tsv
        self._create_temp_directory(os.path.join(dst_dir_path, parser_name), exist_ok=True)
        self.export_df(result_df, dst_file_path)

    def _concatenate_tabularized_chunks(self, src_dir_path: str, dst_dir_path: str, orig_file_name_base: str) -> None:
        # create separate output table for each parser
        for parser_no, parser in enumerate(self.log_parsers, start=1):
            # get directory with tables with data produced by the selected parser
            parser_name = str(parser.short_name)
            parser_tables_dir = os.path.join(src_dir_path, parser_name)

            # get chunk .records file paths
            chunk_tables_file_names = self.get_sorted_chunk_names(src_dir_path=parser_tables_dir)
            chunk_tables_file_paths = [os.path.join(src_dir_path, parser_name, n) for n in chunk_tables_file_names]

            # parse first of the table files paths
            _, _, src_file_ext = self.split_file_path(chunk_tables_file_paths[0])

            # get final table file path
            dst_file_name = f'{orig_file_name_base}.{parser_name}{src_file_ext}'
            dst_file_path = os.path.join(dst_dir_path, dst_file_name)

            # concatenate
            for table_idx, table_file_path in enumerate(chunk_tables_file_paths, start=1):
                self.log.info(f'(file {parser_no}/{len(self.log_parsers)}) Merging file chunk {table_idx} of'
                              f' {len(chunk_tables_file_paths)}')
                with open(dst_file_path, mode='a+') as dst_file:
                    with open(table_file_path) as table_file:
                        if table_idx > 1:
                            next(table_file)
                        lines = table_file.readlines()
                    dst_file.writelines(lines)

    def _concatenate_unparsed_chunks(self, src_dir_path: str, dst_dir_path: str, orig_file_name_base: str) -> None:

        # get file paths of files with unparsed logs
        unparsed_dir_path = os.path.join(src_dir_path, self.unparsed_short_name)
        chunk_unparsed_file_names = self.get_sorted_chunk_names(src_dir_path=unparsed_dir_path)
        chunk_unparsed_file_paths = [os.path.join(src_dir_path, self.unparsed_short_name, n)
                                     for n in chunk_unparsed_file_names]

        # parse first of the unparsed chunk file paths
        src_file_dir, src_file_name, src_file_ext = self.split_file_path(chunk_unparsed_file_paths[0])

        # get final table file path
        dst_file_name = f'{orig_file_name_base}.{self.unparsed_short_name}{src_file_ext}'
        dst_file_path = os.path.join(dst_dir_path, dst_file_name)

        # concatenate unparsed logs
        for file_idx, unparsed_file_path in enumerate(chunk_unparsed_file_paths, start=1):
            self.log.info(f'(file 1/1) Merging file chunk {file_idx} of {len(chunk_unparsed_file_paths)}')
            with open(dst_file_path, mode='a+') as dst_file:
                with open(unparsed_file_path) as unparsed_file:
                    lines = unparsed_file.readlines()
                dst_file.writelines(lines)

    def _create_temp_directory(self, dir_path: str, exist_ok: bool = False, **kwargs) -> None:
        if os.path.exists(dir_path):
            if not exist_ok:
                FileExistsError(f'Cannot create a directory when that directory already exists: `{dir_path}`')
        else:
            self.log.debug(f'Creating directory: {dir_path}')
            os.makedirs(dir_path, exist_ok=exist_ok, **kwargs)

    def _remove_temp_directory(self, dir_path: str) -> None:
        if self.delete_intermediate_result_dirs:
            self.log.debug(f'Removing directory: {dir_path}')
            shutil.rmtree(dir_path)

    @staticmethod
    def split_file_path(file_path: str) -> Tuple[str, str, str]:
        file_dir, file_name = os.path.split(file_path)
        file_name_base, file_name_ext = os.path.splitext(file_name)
        return file_dir, file_name_base, file_name_ext

    @staticmethod
    def get_sorted_chunk_names(src_dir_path: str, mask: str = '.*') -> List[str]:
        # define sort key function
        def key(file_name: str):
            assert (match := FILE_CHUNK_SORT_MASK.match(file_name)), \
                f'File name `{file_name}` does not match the sort key file mask.'
            return int(match.group('id'))

        # get chunk file names matching mask and sort them using custom key
        chunk_names = [f for f in os.listdir(src_dir_path) if re.match(mask, f)]
        sorted_chunk_names = sorted(chunk_names, key=key)

        return sorted_chunk_names
