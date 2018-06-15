#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Scheduler using MPI for running the TESS photometry
pipeline on a large scale multi-core computer.

The setup uses the task-pull paradigm for high-throughput computing
using ``mpi4py``. Task pull is an efficient way to perform a large number of
independent tasks when there are more tasks than processors, especially
when the run times vary for each task.

The basic example was inspired by
https://github.com/jbornschein/mpi4py-examples/blob/master/09-task-pull.py

Example
-------
To run the program using four processes (one master and three workers) you can
execute the following command:

>>> mpiexec -n 4 python mpi_scheduler.py

.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
"""

from __future__ import with_statement, print_function
from mpi4py import MPI
import logging
import os
import enum

#------------------------------------------------------------------------------
if __name__ == '__main__':

	# Get paths to input and output files from environment variables:
	input_folder = os.environ.get('TESSPHOT_INPUT', os.path.join(os.path.dirname(__file__), 'tests', 'input'))
	output_folder = os.environ.get('TESSPHOT_OUTPUT', os.path.abspath('.'))
	todo_file = os.path.join(input_folder, 'todo.sqlite')

	# Define MPI message tags
	tags = enum.IntEnum('tags', ('READY', 'DONE', 'EXIT', 'START'))

	# Initializations and preliminaries
	comm = MPI.COMM_WORLD   # get MPI communicator object
	size = comm.size        # total number of processes
	rank = comm.rank        # rank of this process
	status = MPI.Status()   # get MPI status object

	if rank == 0:
		# Master process executes code below
		from photometry import TaskManager

		with TaskManager(todo_file) as tm:
			# Get list of tasks:
			numtasks = tm.get_number_tasks()
			tm.logger.info("%d tasks to be run", numtasks)

			# Start the master loop that will assign tasks
			# to the workers:
			num_workers = size - 1
			closed_workers = 0
			tm.logger.info("Master starting with %d workers", num_workers)
			while closed_workers < num_workers:
				# Ask workers for information:
				data = comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)
				source = status.Get_source()
				tag = status.Get_tag()

				if tag == tags.READY:
					# Worker is ready, so send it a task
					task = tm.get_task()

					if task:
						task_index = task['priority']
						tm.start_task(task_index)
						comm.send(task, dest=source, tag=tags.START)
						tm.logger.info("Sending task %d to worker %d", task_index, source)
					else:
						comm.send(None, dest=source, tag=tags.EXIT)

				elif tag == tags.DONE:
					# The worker is done with a task
					tm.logger.info("Got data from worker %d: %s", source, data)
					tm.save_result(data)

				elif tag == tags.EXIT:
					# The worker has exited
					tm.logger.info("Worker %d exited.", source)
					closed_workers += 1

				else:
					# This should never happen, but just to
					# make sure we don't run into an infinite loop:
					raise Exception("Master recieved an unknown tag: '{0}'".format(tag))

		tm.logger.info("Master finishing")

	else:
		# Worker processes execute code below
		from photometry import tessphot
		from timeit import default_timer

		# Configure logging within photometry:
		formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
		console = logging.StreamHandler()
		console.setFormatter(formatter)
		logger = logging.getLogger('photometry')
		logger.addHandler(console)
		logger.setLevel(logging.WARNING)

		try:
			k = 0
			while True:
				# Send signal that we are ready for task,
				# and receive a task from the master:
				comm.send(None, dest=0, tag=tags.READY)
				task = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
				tag = status.Get_tag()

				if tag == tags.START:
					# Do the work here
					result = task.copy()
					del task['priority']

					t1 = default_timer()
					try:
						pho = tessphot(input_folder=input_folder, output_folder=output_folder, plot=True, **task)
					except:
						logger.exception("Fatal error in call to tessphot")
					t2 = default_timer()

					# Construct result message:
					result.update({
						'status': pho.status,
						'time': t2 - t1,
						'details': pho._details
					})

					# Send the result back to the master:
					comm.send(result, dest=0, tag=tags.DONE)

					# Attempt some cleanup:
					# TODO: Is this even needed?
					del pho, task, result
					k += 1

				elif tag == tags.EXIT:
					# We were told to EXIT, so lets do that
					break

				else:
					# This should never happen, but just to
					# make sure we don't run into an infinite loop:
					raise Exception("Worker recieved an unknown tag: '{0}'".format(tag))

		except:
			logger.exception("Something failed in worker")

		finally:
			comm.send(None, dest=0, tag=tags.EXIT)
