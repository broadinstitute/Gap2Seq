#!/usr/bin/env python
# -*- coding: utf-8 -*-

##############################################################################
#  Gap2Seq
#  Copyright (C) Leena Salmela, Kristoffer Sahlin, Veli Mäkinen,
#  Alexandru Tomescu, Riku Walve 2017
#
#  Contact: leena.salmela@cs.helsinki.fi
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as
#  published by the Free Software Foundation, either version 3 of the
#  License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
##############################################################################

from __future__ import print_function
import os, sys
import subprocess, multiprocessing
import datetime

script_dir = os.path.dirname(os.path.realpath(__file__))
isexecutable = lambda f: os.path.isfile(f) and os.access(f, os.X_OK)
def find_executable(path_hints, name):
    for path_hint in path_hints:
        path = os.path.join(path_hint, name)
        if isexecutable(path):
            return path

    print('%s not found' % name, file=sys.stderr)
    sys.exit(1)

# Find required tools
GAPMERGER = find_executable([script_dir, './', '../'], 'GapMerger')
GAPCUTTER = find_executable([script_dir, './', '../'], 'GapCutter')
GAP2SEQ = find_executable([script_dir, './', '../'], 'Gap2Seq-core')
READFILTER = find_executable([script_dir, './', '../'], 'ReadFilter')

# An object for holding all the data for a library of short reads
class Library:
    def __init__(self, bam, read_length, mean_insert_size, std_dev, threshold):
        self.bam, self.len, self.mu,  self.sd, self.threshold = \
            bam, read_length, mean_insert_size, std_dev, threshold

        # Assert bam-file is indexed
        if not os.path.isfile(bam + '.bai'):
            print('%s.bai not found' % bam, file=sys.stderr)
            sys.exit(1)

    def data(self):
        return ['-bam', self.bam,
            '-read-length', str(self.len),
            '-mean', str(self.mu),
            '-std-dev', str(self.sd)]

class Gap:
    def __init__(self, scaffold, position, length, left, right, comment, id):
        self.scaffold, self.position = scaffold, position
        self.left, self.right, self.length = left, right, length
        self.comment, self.id = comment, id

    def data(self):
        return ['-scaffold', str(self.scaffold),
            '-breakpoint', str(self.position),
            '-gap-length', str(self.length)]

    def filler_data(self):
        return ['-left', self.left,
            '-right', self.right,
            '-length', str(self.length)]

# Listener for filled gaps, to print progress
successful_gaps, filled_gaps, num_of_gaps = 0, 0, 1
def listener(queue, filename):
    global successful_gaps, filled_gaps, num_of_gaps
    start_time = datetime.datetime.now()
    with open(filename, 'w') as f:
        while 1:
            result = queue.get()
            if result == 'kill':
                break

            success, comment, fill = result

            filled_gaps += 1
            if success:
                successful_gaps += 1

            delta = (datetime.datetime.now() - start_time).total_seconds()
            eta = (delta / filled_gaps) * (num_of_gaps - filled_gaps)
            eta_string = str(datetime.timedelta(seconds=eta))

            print('Progress %.3f%% [%i / %i] %s left\t' % \
                (100*(filled_gaps / num_of_gaps), filled_gaps, num_of_gaps, eta_string),
                end='\r')

            f.write(comment + '\n' + fill + '\n')
            f.flush()
    return successful_gaps

# Runs all the read filtering and gap filling for a single gap
def fill_gap(libraries, gap, k, fuz, solid, derr, max_mem, reads=None, queue=None):
    # Cleanup, just to be sure
    reads_base = 'tmp.reads.' + gap.id + '.'
    subprocess.check_call(['rm', '-f', reads_base + '*'])

    # TODO: Get a more accurate value?
    flank_length = str(k + fuz)

    # Extract reads
    extracted = False
    if reads == None:
        extracted = True
        reads = []
        with open('tmp.extract.' + gap.id + '.log', 'w') as f:
            filtered_length = 0
            for i, lib in enumerate(libraries):
                reads_file = reads_base + str(i)
                subprocess.check_call([READFILTER,
                    '-reads', reads_file,
                    '-flank-length', flank_length] + \
                    gap.data() + lib.data(), stderr=f, stdout=f)

                # If no reads are extracted, no file exists
                if not os.path.isfile(reads_file):
                    continue

                reads.append(reads_file)

                grep = subprocess.check_output('grep \'^[^>;]\' ' + reads_file + ' | wc -c', shell=True)
                filtered_length += int(grep)

            # Thresholding has to be done here in case of multiple libraries
            threshold = sum([lib.threshold for lib in libraries])
            if (filtered_length / gap.length) < threshold:
                for i, lib in enumerate(libraries):
                    reads_file = reads_base + str(i) + '.unmapped'
                    subprocess.check_call([READFILTER,
                        '-unmapped-only',
                        '-reads', reads_file] + gap.data() + lib.data(),
                        stderr=f, stdout=f)

                    if os.path.isfile(reads_file):
                        reads.append(reads_file)

    # Run Gap2Seq on the gap with the filtered reads
    log = ''
    with open('tmp.gap2seq.' + gap.id + '.log', 'w') as f:
        wd = os.getcwd()
        wd_new = wd + '/tmp.' + gap.id
        subprocess.check_call(['mkdir', wd_new])
        os.chdir(wd_new)
        reads_final = []
        for r in reads:
            reads_final.append(os.path.join('..', r))
        log = subprocess.check_output([os.path.join('..', GAP2SEQ),
            '-k', str(k),
            '-fuz', str(fuz),
            '-solid', str(solid),
            '-nb-cores', '1',
            '-dist-error', str(derr),
            '-max-mem', str(max_mem),
            '-reads', ','.join(reads_final)] + gap.filler_data(),
            stderr=f)
        os.chdir(wd)
        subprocess.check_call(['rm', '-r', wd_new])

    # Gap2Seq output:
    #  143 lines of graph information
    #  1-2 lines of gap information
    #  Filled sequence
    #  'Gap2Seq'

    filled = False
    fill = log.split(b'\n')
    if len(fill) > 147:
        filled = True
        fill = fill[-3].decode()
    else:
        fill = gap.left + ('N' * gap.length) + gap.right

    # Cleanup reads
    if extracted:
        subprocess.check_call(['rm', '-f'] + reads)

    # Remove logs and temporary/intermediate files
    subprocess.check_call(['rm', '-f',
        'tmp.extract.' + gap.id + '.log',
        'tmp.gap2seq.' + gap.id + '.log'])

    if queue != None:
        queue.put((filled, gap.comment, fill))
    else:
        return (filled, gap.comment, fill)

# NOTE: Assumes gaps and the bed file are in the same order
def parse_gap(bed, gap, id):
    gap = gap.split('\n')
    comment = gap[0]

    gap = ''.join(gap[1:])

    left = gap[:gap.upper().find('N')]
    right = gap[gap.upper().rfind('N')+1:]
    length = len(gap) - len(left) - len(right)

    # Parse gap data from bed file
    gap_data = bed.readline().rstrip().split('\t')
    scaffold = gap_data[0]
    position = int(gap_data[1]) + len(left)

    return Gap(scaffold, position, length, left, right, comment, id)

# Starts multiple gapfilling processes in parallel
def start_fillers(bed, gaps, libraries, queue=None, pool=None, k=31, fuz=10,
        solid=2, derr=500, max_mem=20, reads=None):
    start_filler = lambda seq, gap_id: fill_gap(libraries, parse_gap(bed, seq,
        str(gap_id)), k, fuz, solid, derr, max_mem, reads)

    if pool != None:
        start_filler = lambda seq, gap_id: pool.apply_async(fill_gap,
            args=([libraries, parse_gap(bed, seq, str(gap_id)), k, fuz,
                solid, derr, max_mem, reads, queue]))

    gap_id = 0

    jobs = []
    seq = ''
    for gap in gaps:
        if gap[0] == '>' and seq != '':
            jobs.append(start_filler(seq, gap_id))
            gap_id += 1
            seq = ''
        seq += gap

    jobs.append(start_filler(seq, gap_id))

    return jobs

# Run GapCutter, i.e. cut scaffolds into contigs and gaps
def cut_gaps(scaffolds, contigs_file = 'tmp.contigs', gap_file = 'tmp.gaps',
        bed_file = 'tmp.bed'):
    if os.path.isfile(contigs_file):
        print('%s exists' % contigs_file)
        sys.exit(1)
    if os.path.isfile(gap_file):
        print('%s exists' % gap_file)
        sys.exit(1)
    if os.path.isfile(bed_file):
        print('%s exists' % bed_file)
        sys.exit(1)

    subprocess.check_call([GAPCUTTER,
            '-scaffolds', scaffolds,
            '-gaps', gap_file,
            '-contigs', contigs_file,
            '-bed', bed_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return open(bed_file, 'r'), open(gap_file, 'r')

# Run GAPMERGER, i.e. merge contigs and filled gaps back into scaffolds
def merge_gaps(filled, merged, contigs_file='tmp.contigs'):
    subprocess.check_call([GAPMERGER,
            '-scaffolds', merged,
            '-gaps', filled,
            '-contigs', contigs_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # subprocess.check_call(['rm', '-f', contigs_file, filled])

# Parse VCF file and extract kmers from reference genome
def cut_vcf(vcf, reference_file, k, fuz, contigs_file = 'tmp.contigs',
        gap_file = 'tmp.gaps', bed_file = 'tmp.bed'):
    # Parse chromosomes from the reference into a dictionary
    # NOTE: Since this loads the entire reference genome into RAM, we will
    # expect the genome to be short enough.
    reference = {}
    comment, seq = '', ''
    for line in f:
        if line[0] == '>':
            if seq != '':
                reference[comment] = seq
                seq = ''
            comment = line.rstrip()[1:]
        seq += line.rstrip()
    reference[comment] = seq

    # Open a new gap and bed files
    gap, bed = open(gap_file, 'r+'), open(bed_file, 'r+')
    for line in f:
        if line[0] == '#': continue
        fields = line.rstrip().split('\t')

        # Parse VCF fields
        insert_ref = fields[3]
        insert = fields[4][len(insert_ref):]
        comment, start, end = fields[0], int(fields[1]) - 1, int(fields[1]) + len(insert) - 1

        # Extract kmers from reference seqeuences
        left = reference[comment][start:start+k+fuz]
        right = reference[comment][end-(k+fuz):end]
        seq = left + 'N' * len(insert) + right

        gap.write('>%s:%i-%i\n%s' % (comment, start, end, seq))
        bed.write('%s\t%i\t%i' % (comment, start-(k+fuz)+1, end+(k+fuz)+1))

    # We will read the files later, so they need to be seeked to beginning
    gap.seek(0)
    bed.seek(0)

    return gap, bed

# Count the number of gaps
def count_gaps(bed):
    global num_of_gaps
    num_of_gaps = 0

    bed.seek(0)
    for line in bed:
        num_of_gaps += 1
    bed.seek(0)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Gap2Seq 3.0')

    # filler.py specific arguments
    parser.add_argument('-f', '--filled', required=True, type=str, help="output file for filled scaffolds")
    parser.add_argument('-t', '--threads', type=int, default=1, help="number of threads to use")

    # Gap2Seq specific arguments
    parser.add_argument('-k', type=int, default=31, help="kmer length for DBG  [default 31]")
    parser.add_argument('--fuz', type=int, default=10, help="number of nucleotides to ignore on gap fringes  [default 10]")
    parser.add_argument('--solid', type=int, default=2, help="threshold for solid k-mers for building the DBG [default 2]")
    parser.add_argument('--max-mem', type=float, default=20, help="maximum memory usage of DP table computation in gigabytes (excluding DBG) [default 20]")
    parser.add_argument('--dist-error', type=int, default=500, help="maximum error in gap estimates  [default 500]")

    # Either a set of mapped read libraries or a set of fasta-formatted reads
    # Tab-separated list:
    # bam, read_length, mean_insert_size, std_dev, threshold
    parser.add_argument('-l', '--libraries', type=str, help="List of aligned read libraries")
    parser.add_argument('-i', '--index', type=int, default=-1, help=argparse.SUPPRESS)

    parser.add_argument('-r', '--reads', type=str, help="short reads")

    # One of three options is required for gap data:
    # 1. Cut gaps and bed from some scaffolds
    parser.add_argument('-s', '--scaffolds', type=str, help="")

    # 2. Use pre-cut gaps and bed
    parser.add_argument('-b', '--bed', type=argparse.FileType('r'), help="")
    parser.add_argument('-g', '--gaps', type=argparse.FileType('r'), help="")

    # 3. Generate gaps and bed from VCF
    parser.add_argument('-v', '--vcf', type=argparse.FileType('r'), help=argparse.SUPPRESS)
    parser.add_argument('-R', '--reference', type=argparse.FileType('r'), help=argparse.SUPPRESS)

    args = vars(parser.parse_args())

    # Short read libraries aligned to the scaffolds
    libraries = []
    if args['libraries'] != None:
        with open(args['libraries'], 'r') as f:
            for lib in f:
                arg = lib.split('\t')
                libraries += [Library(arg[0], int(arg[1]), int(arg[2]), int(arg[3]), int(arg[4]))]

        # Use only 1 library
        if args['index'] != -1:
            libraries = [libraries[args['index']]]
    elif args['reads'] != None:
        args['reads'] = args['reads'].split(',')
    else:
        parser.print_help()
        print('Either [-r/--reads], or [-l/--libraries] is required.')
        exit(1)

    scaffolds_cut = False
    if args['bed'] == None or args['gaps'] == None:
        if args['scaffolds'] != None:
            print('Cutting gaps')
            args['bed'], args['gaps'] = cut_gaps(args['scaffolds'])
            scaffolds_cut = True
            args['final_out'] = args['filled']
            args['filled'] = 'tmp.filled'
        elif args['vcf'] != None:
            print('Parsing VCF')
            args['bed'], args['gaps'] = cut_vcf(args['vcf'], args['reference'], args['k'], args['fuz'])
        else:
            parser.print_help()
            print('Either [-s/--scaffolds], [-b/--bed and -g/--gaps], or [-v/--vcf and -R/--reference] are required.')
            exit(1)

    count_gaps(args['bed'])

    # Gap2Seq divides the max mem evenly between threads, but as we run multiple
    # parallel instances with 1 thread, we need to pre-divide
    max_mem = args['max_mem'] / args['threads']

    queue, pool = None, None
    if args['threads'] > 1:
        manager = multiprocessing.Manager()
        queue = manager.Queue()
        pool = multiprocessing.Pool(args['threads'] + 1)

    # Start listening for filled gaps
    if args['threads'] > 1:
        res = pool.apply_async(listener, (queue, args['filled']))

    print('Starting gapfillers')
    jobs = start_fillers(args['bed'], args['gaps'], libraries, queue=queue,
        pool=pool, k=args['k'], fuz=args['fuz'], solid=args['solid'],
        derr=args['dist_error'], max_mem=max_mem, reads=args['reads'])

    args['bed'].close()
    args['gaps'].close()

    if args['threads'] > 1:
        for job in jobs:
            job.get()
        queue.put('kill')
        successful_gaps=res.get(timeout=1)
        pool.close()
        pool.join()

    if scaffolds_cut:
        print('Merging filled gaps and contigs')
        merge_gaps(args['filled'], args['final_out'])

    print('Filled %i out of %i gaps' % (successful_gaps, num_of_gaps))
