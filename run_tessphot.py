#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Command-line utility to run TESS photometry of single star.

.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
"""

from __future__ import with_statement, print_function
import os
import argparse
import logging
import multiprocessing
from photometry import tessphot, TaskManager

#------------------------------------------------------------------------------
if __name__ == '__main__':

	logging_level = logging.INFO

	parser = argparse.ArgumentParser(description='Run TESS Photometry pipeline on single star.')
	parser.add_argument('-m', '--method', help='Photometric method to use.', default=None, choices=('aperture', 'psf', 'linpsf'))
	parser.add_argument('-d', '--debug', help='Print debug messages.', action='store_true')
	parser.add_argument('-q', '--quiet', help='Only report warnings and errors.', action='store_true')
	parser.add_argument('-p', '--plot', help='Save plots when running.', action='store_true')
	parser.add_argument('-r', '--random', help='Run on random target from TODO-list.', action='store_true')
	parser.add_argument('-a', '--all', help='Run on all targets from TODO-list.', action='store_true')
	parser.add_argument('starid', type=int, help='TIC identifier of target.', nargs='?', default=None)
	args = parser.parse_args()

	if args.starid is None and not args.random and not args.all:
		parser.error("Please select either a specific STARID, RANDOM or ALL.")

	if args.quiet:
		logging_level = logging.WARNING
	elif args.debug:
		logging_level = logging.DEBUG

	# Setup logging:
	formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
	console = logging.StreamHandler()
	console.setFormatter(formatter)
	logger = logging.getLogger(__name__)
	logger.addHandler(console)
	logger.setLevel(logging_level)
	logger_parent = logging.getLogger('photometry')
	logger_parent.addHandler(console)
	logger_parent.setLevel(logging_level)

	# Get input and output folder from enviroment variables:
	input_folder = os.environ.get('TESSPHOT_INPUT', os.path.abspath(os.path.join(os.path.dirname(__file__), 'tests', 'input')))
	output_folder = os.environ.get('TESSPHOT_OUTPUT', os.path.abspath('.'))
	logger.info("Loading input data from '%s'", input_folder)
	logger.info("Putting output data in '%s'", output_folder)

	# Run the program:
	with TaskManager(input_folder) as tm:
		if args.starid is not None:
			task = {'starid': args.starid, 'method': args.method}
			pho = tessphot(input_folder=input_folder, output_folder=output_folder, plot=args.plot, **task)

		elif args.random:
			task = tm.get_random_task()
			del task['priority']
			pho = tessphot(input_folder=input_folder, output_folder=output_folder, plot=args.plot, **task)

		elif args.all:
			# TODO: Put up a multiprocessing Pool and run this in parallel
			pool = multiprocessing.Pool()

			task = tm.get_task()
			del task['priority']
			pho = tessphot(input_folder=input_folder, output_folder=output_folder, plot=args.plot, **task)

			# Close multiprocessing pool:
			pool.close()
			pool.join()

	# TODO: Write out the results?
	if not args.quiet:
		print("=======================")
		print("STATUS: {0}".format(pho.status.name))
