#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Create the TODO list which is used by the pipeline to keep track of the
targets that needs to be processed.
"""

from __future__ import division, with_statement, print_function, absolute_import
import six
from six.moves import map
import os
import numpy as np
import logging
import sqlite3
import warnings
warnings.filterwarnings('ignore', category=FutureWarning, module='h5py')
import h5py
import re
import functools
import contextlib
from astropy.table import Table, vstack
from astropy.io import fits
from astropy.wcs import WCS
from timeit import default_timer
from .utilities import find_tpf_files, find_hdf5_files, find_catalog_files, sphere_distance
import multiprocessing

def calc_cbv_area(catalog_row, settings):
	# The distance from the camera centre to the corner furthest away:
	camera_radius = np.sqrt( 12**2 + 12**2 ) # np.max(sphere_distance(a[:,0], a[:,1], settings['camera_centre_ra'], settings['camera_centre_dec']))

	# Distance to centre of the camera in degrees:
	camera_centre_dist = sphere_distance(catalog_row['ra'], catalog_row['decl'], settings['camera_centre_ra'], settings['camera_centre_dec'])

	cbv_area = settings['camera']*100 + settings['ccd']*10

	if camera_centre_dist < 0.25*camera_radius:
		cbv_area += 1
	elif camera_centre_dist < 0.5*camera_radius:
		cbv_area += 2
	elif camera_centre_dist < 0.75*camera_radius:
		cbv_area += 3
	else:
		cbv_area += 4

	return cbv_area

def _ffi_todo_wrapper(args):
	return _ffi_todo(*args)

def _ffi_todo(input_folder, sector, camera, ccd):

	logger = logging.getLogger(__name__)

	cat_tmp = []

	# See if there are any FFIs for this camera and ccd.
	# We just check if an HDF5 file exist.
	hdf5_file = find_hdf5_files(input_folder, sector=sector, camera=camera, ccd=ccd)
	if len(hdf5_file) != 1:
		raise IOError("Could not find HDF5 file")

	# Load the relevant information from the HDF5 file for this camera and ccd:
	with h5py.File(hdf5_file[0], 'r') as hdf:
		if isinstance(hdf['wcs'], h5py.Group):
			refindx = hdf['wcs'].attrs['ref_frame']
			hdr_string = hdf['wcs']['%04d' % refindx][0]
		else:
			hdr_string = hdf['wcs'][0]
		if not isinstance(hdr_string, six.string_types): hdr_string = hdr_string.decode("utf-8") # For Python 3
		wcs = WCS(header=fits.Header().fromstring(hdr_string))
		offset_rows = hdf['images'].attrs.get('PIXEL_OFFSET_ROW', 0)
		offset_cols = hdf['images'].attrs.get('PIXEL_OFFSET_COLUMN', 0)
		image_shape = hdf['images']['0000'].shape

	# Load the corresponding catalog:
	catalog_file = find_catalog_files(input_folder, sector=sector, camera=camera, ccd=ccd)
	if len(catalog_file) != 1:
		raise IOError("Catalog file not found: SECTOR=%s, CAMERA=%s, CCD=%s" % (sector, camera, ccd))

	with contextlib.closing(sqlite3.connect(catalog_file[0])) as conn:
		conn.row_factory = sqlite3.Row
		cursor = conn.cursor()

		# Load the settings:
		cursor.execute("SELECT * FROM settings WHERE camera=? AND ccd=? LIMIT 1;", (camera, ccd))
		settings = cursor.fetchone()

		# Find all the stars in the catalog brigher than a certain limit:
		cursor.execute("SELECT starid,tmag,ra,decl FROM catalog WHERE tmag < 15 ORDER BY tmag;")
		for row in cursor.fetchall():
			logger.debug("%011d - %.3f", row['starid'], row['tmag'])

			# Calculate the position of this star on the CCD using the WCS:
			ra_dec = np.atleast_2d([row['ra'], row['decl']])
			x, y = wcs.all_world2pix(ra_dec, 0)[0]

			# Subtract the pixel offset if there is one:
			x -= offset_cols
			y -= offset_rows

			# If the target falls outside silicon, do not add it to the todo list:
			# The reason for the strange 0.5's is that pixel centers are at integers.
			if x < -0.5 or y < -0.5 or x > image_shape[1]-0.5 or y > image_shape[0]-0.5:
				continue

			# Calculate the Cotrending Basis Vector area the star falls in:
			cbv_area = calc_cbv_area(row, settings)

			# The targets is on silicon, so add it to the todo list:
			cat_tmp.append({
				'starid': row['starid'],
				'sector': sector,
				'camera': camera,
				'ccd': ccd,
				'datasource': 'ffi',
				'tmag': row['tmag'],
				'cbv_area': cbv_area
			})

		cursor.close()

	# Create the TODO list as a table which we will fill with targets:
	return Table(
		rows=cat_tmp,
		names=('starid', 'sector', 'camera', 'ccd', 'datasource', 'tmag', 'cbv_area'),
		dtype=('int64', 'int32', 'int32', 'int32', 'S256', 'float32', 'int32')
	)

#------------------------------------------------------------------------------
def _tpf_todo(fname, input_folder=None, cameras=None, ccds=None, find_secondary_targets=True, exclude=[]):

	logger = logging.getLogger(__name__)

	# Create the TODO list as a table which we will fill with targets:
	cat_tmp = []
	empty_table = Table(
		names=('starid', 'sector', 'camera', 'ccd', 'datasource', 'tmag', 'cbv_area'),
		dtype=('int64', 'int32', 'int32', 'int32', 'S256', 'float32', 'int32')
	)

	logger.debug("Processing TPF file: '%s'", fname)
	with fits.open(fname, memmap=True, mode='readonly') as hdu:
		starid = hdu[0].header['TICID']
		sector = hdu[0].header['SECTOR']
		camera = hdu[0].header['CAMERA']
		ccd = hdu[0].header['CCD']

		if (starid, sector, 'tpf') in exclude or (starid, sector, 'all') in exclude:
			logger.debug("Target excluded: STARID=%d, SECTOR=%d, DATASOURCE=tpf", starid, sector)
			return empty_table

		if camera in cameras and ccd in ccds:
			# Load the corresponding catalog:
			catalog_file = find_catalog_files(input_folder, sector=sector, camera=camera, ccd=ccd)
			if len(catalog_file) != 1:
				raise IOError("Catalog file not found: SECTOR=%s, CAMERA=%s, CCD=%s" % (sector, camera, ccd))

			with contextlib.closing(sqlite3.connect(catalog_file[0])) as conn:
				conn.row_factory = sqlite3.Row
				cursor = conn.cursor()

				cursor.execute("SELECT * FROM settings WHERE camera=? AND ccd=? LIMIT 1;", (camera, ccd))
				settings = cursor.fetchone()
				if settings is None:
					logger.error("Settings could not be loaded for camera=%d, ccd=%d.", camera, ccd)
					raise ValueError("Settings could not be loaded for camera=%d, ccd=%d." % (camera, ccd))

				# Get information about star:
				cursor.execute("SELECT * FROM catalog WHERE starid=? LIMIT 1;", (starid, ))
				row = cursor.fetchone()
				if row is None:
					logger.error("Starid %d was not found in catalog (camera=%d, ccd=%d).", starid, camera, ccd)
					return empty_table

				# Calculate CBV area that target falls in:
				cbv_area = calc_cbv_area(row, settings)

				# Add the main target to the list:
				cat_tmp.append({
					'starid': starid,
					'sector': sector,
					'camera': camera,
					'ccd': ccd,
					'datasource': 'tpf',
					'tmag': row['tmag'],
					'cbv_area': cbv_area
				})

				if find_secondary_targets:
					# Load all other targets in this stamp:
					# Use the WCS of the stamp to find all stars that fall within
					# the footprint of the stamp.
					image_shape = hdu[2].shape
					wcs = WCS(header=hdu[2].header)
					footprint = wcs.calc_footprint(center=False)
					radec_min = np.min(footprint, axis=0)
					radec_max = np.max(footprint, axis=0)
					# TODO: This can fail to find all targets e.g. if the footprint is across the ra=0 line
					cursor.execute("SELECT * FROM catalog WHERE ra BETWEEN ? AND ? AND decl BETWEEN ? AND ? AND starid != ? AND tmag < 15;", (radec_min[0], radec_max[0], radec_min[1], radec_max[1], starid))
					for row in cursor.fetchall():
						# Calculate the position of this star on the CCD using the WCS:
						ra_dec = np.atleast_2d([row['ra'], row['decl']])
						x, y = wcs.all_world2pix(ra_dec, 0)[0]

						# If the target falls outside silicon, do not add it to the todo list:
						# The reason for the strange 0.5's is that pixel centers are at integers.
						if x < -0.5 or y < -0.5 or x > image_shape[1]-0.5 or y > image_shape[0]-0.5:
							continue

						# Add this secondary target to the list:
						# Note that we are storing the starid of the target
						# in which target pixel file the target can be found.
						logger.debug("Adding extra target: TIC %d", row['starid'])
						cat_tmp.append({
							'starid': row['starid'],
							'sector': sector,
							'camera': camera,
							'ccd': ccd,
							'datasource': 'tpf:' + str(starid),
							'tmag': row['tmag'],
							'cbv_area': cbv_area
						})

				# Close the connection to the catalog SQLite database:
				cursor.close()

	# TODO: Could we avoid fixed-size strings in datasource column?
	return Table(
		rows=cat_tmp,
		names=('starid', 'sector', 'camera', 'ccd', 'datasource', 'tmag', 'cbv_area'),
		dtype=('int64', 'int32', 'int32', 'int32', 'S256', 'float32', 'int32')
	)

#------------------------------------------------------------------------------
def make_todo(input_folder=None, cameras=None, ccds=None, overwrite=False):
	"""
	Create the TODO list which is used by the pipeline to keep track of the
	targets that needs to be processed.

	Will create the file `todo.sqlite` in the directory.

	Parameters:
		input_folder (string, optional): Input folder to create TODO list for.
			If ``None``, the input directory in the environment variable ``TESSPHOT_INPUT`` is used.
		cameras (iterable of integers, optional): TESS camera number (1-4). If ``None``, all cameras will be included.
		ccds (iterable of integers, optional): TESS CCD number (1-4). If ``None``, all cameras will be included.
		overwrite (boolean): Overwrite existing TODO file. Default=``False``.

	Raises:
		IOError: If the specified ``input_folder`` is not an existing directory.

	.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
	"""

	logger = logging.getLogger(__name__)

	# Check the input folder, and load the default if not provided:
	if input_folder is None:
		input_folder = os.environ.get('TESSPHOT_INPUT', os.path.join(os.path.dirname(__file__), 'tests', 'input'))

	# Check that the given input directory is indeed a directory:
	if not os.path.isdir(input_folder):
		raise IOError("The given path does not exist or is not a directory")

	# Make sure cameras and ccds are iterable:
	cameras = (1, 2, 3, 4) if cameras is None else (cameras, )
	ccds = (1, 2, 3, 4) if ccds is None else (ccds, )

	# The TODO file that we want to create. Delete it if it already exits:
	todo_file = os.path.join(input_folder, 'todo.sqlite')
	if os.path.exists(todo_file):
		if overwrite:
			os.remove(todo_file)
		else:
			logger.info("TODO file already exists")
			return

	# Number of threads available for parallel processing:
	threads_max = int(os.environ.get('SLURM_CPUS_PER_TASK', multiprocessing.cpu_count()))

	# Load file with targets to be excluded from processing for some reason:
	exclude_file = os.path.join(os.path.dirname(__file__), 'data', 'todolist-exclude.dat')
	exclude = np.genfromtxt(exclude_file, usecols=(0,1,2), dtype=None, encoding='utf-8')
	exclude = set([tuple(e) for e in exclude])

	# Create the TODO list as a table which we will fill with targets:
	cat = Table(
		names=('starid', 'sector', 'camera', 'ccd', 'datasource', 'tmag', 'cbv_area'),
		dtype=('int64', 'int32', 'int32', 'int32', 'S256', 'float32', 'int32')
	)

	# Load list of all Target Pixel files in the directory:
	tpf_files = find_tpf_files(input_folder)
	logger.info("Number of TPF files: %d", len(tpf_files))

	if len(tpf_files) > 0:
		# Open a pool of workers:
		logger.info("Starting pool of workers for TPFs...")
		threads = min(threads_max, len(tpf_files)) # No reason to use more than the number of jobs in total
		logger.info("Using %d processes.", threads)

		if threads > 1:
			pool = multiprocessing.Pool(threads)
			m = pool.imap_unordered
		else:
			m = map

		# Run the TPF files in parallel:
		tic = default_timer()
		_tpf_todo_wrapper = functools.partial(_tpf_todo, input_folder=input_folder, cameras=cameras, ccds=ccds, find_secondary_targets=False, exclude=exclude)
		for cat2 in m(_tpf_todo_wrapper, tpf_files):
			cat = vstack([cat, cat2], join_type='exact')

		if threads > 1:
			pool.close()
			pool.join()

		# Amount of time it took to process TPF files:
		toc = default_timer()
		logger.info("Elaspsed time: %f seconds (%f per file)", toc-tic, (toc-tic)/len(tpf_files))

		# Remove secondary TPF targets if they are also the primary target:
		indx_remove = np.zeros(len(cat), dtype='bool')
		cat.add_index('starid')
		for k, row in enumerate(cat):
			if row['datasource'].startswith('tpf:'):
				indx = cat.loc['starid', row['starid']]['datasource'] == 'tpf'
				if np.any(indx):
					indx_remove[k] = True
		cat.remove_indices('starid')
		logger.info("Removing %d secondary TPF files as they are also primary", np.sum(indx_remove))
		cat = cat[~indx_remove]

	# Find list of all HDF5 files:
	hdf_files = find_hdf5_files(input_folder, camera=cameras, ccd=ccds)
	logger.info("Number of HDF5 files: %d", len(hdf_files))

	if len(hdf_files) > 0:
		# TODO: Could we change this so we dont have to parse the filename?
		inputs = []
		for fname in hdf_files:
			m = re.match(r'sector(\d+)_camera(\d)_ccd(\d)\.hdf5', os.path.basename(fname))
			inputs.append( (input_folder, int(m.group(1)), int(m.group(2)), int(m.group(3))) )

		# Open a pool of workers:
		logger.info("Starting pool of workers for FFIs...")
		threads = min(threads_max, len(inputs)) # No reason to use more than the number of jobs in total
		logger.info("Using %d processes.", threads)

		if threads > 1:
			pool = multiprocessing.Pool(threads)
			m = pool.imap_unordered
		else:
			m = map

		tic = default_timer()
		ccds_done = 0
		for cat2 in m(_ffi_todo_wrapper, inputs):
			cat = vstack([cat, cat2], join_type='exact')
			ccds_done += 1
			logger.info("CCDs done: %d/%d", ccds_done, len(inputs))

		# Amount of time it took to process TPF files:
		toc = default_timer()
		logger.info("Elaspsed time: %f seconds (%f per file)", toc-tic, (toc-tic)/len(inputs))

		if threads > 1:
			pool.close()
			pool.join()

	# Check if any targets were found:
	if len(cat) == 0:
		logger.error("No targets found")
		return

	# Remove duplicates!
	logger.info("Removing duplicate entries...")
	_, idx = np.unique(cat[('starid', 'sector', 'camera', 'ccd', 'datasource')], return_index=True, axis=0)
	cat = cat[np.sort(idx)]

	# Exclude targets from FFIs:
	# Add an index and use that to search for starid, and then further check sector and datasource:
	cat.add_index('starid')
	remove_indx = []
	for ex in exclude:
		try:
			indx = np.atleast_1d(cat.loc_indices['starid', ex[0]])
		except KeyError:
			indx = []
		for i in indx:
			if cat[i]['sector'] == ex[1] and cat[i]['datasource'] == ex[2]:
				remove_indx.append(i)
	if remove_indx:
		del cat[remove_indx]
	cat.remove_indices('starid')

	# Load file with specific method settings and create lookup-table of them:
	methods_file = os.path.join(os.path.dirname(__file__), 'data', 'todolist-methods.dat')
	methods_file = np.genfromtxt(methods_file, usecols=(0,1,2,3), dtype=None, encoding='utf-8')
	methods = {}
	for m in methods_file:
		methods[(m[0], m[1], m[2])] = m[3].strip().lower()

	# Sort the final list:
	cat.sort('tmag')

	# Write the TODO list to the SQLite database file:
	logger.info("Writing TODO file...")
	with contextlib.closing(sqlite3.connect(todo_file)) as conn:
		cursor = conn.cursor()

		cursor.execute("""CREATE TABLE todolist (
			priority BIGINT NOT NULL,
			starid BIGINT NOT NULL,
			sector INT NOT NULL,
			datasource TEXT NOT NULL DEFAULT 'ffi',
			camera INT NOT NULL,
			ccd INT NOT NULL,
			method TEXT DEFAULT NULL,
			tmag REAL,
			status INT DEFAULT NULL,
			cbv_area INT NOT NULL
		);""")

		for pri, row in enumerate(cat):
			# Find if there is a specific method defined for this target:
			method = methods.get((int(row['starid']), int(row['sector']), row['datasource'].strip()), None)

			# Add target to TODO-list:
			cursor.execute("INSERT INTO todolist (priority,starid,sector,camera,ccd,datasource,tmag,cbv_area,method) VALUES (?,?,?,?,?,?,?,?,?);", (
				pri+1,
				int(row['starid']),
				int(row['sector']),
				int(row['camera']),
				int(row['ccd']),
				row['datasource'].strip(),
				float(row['tmag']),
				int(row['cbv_area']),
				method
			))

		conn.commit()
		cursor.execute("CREATE UNIQUE INDEX priority_idx ON todolist (priority);")
		cursor.execute("CREATE INDEX starid_datasource_idx ON todolist (starid, datasource);") # FIXME: Should be "UNIQUE", but something is weird in ETE-6?!
		cursor.execute("CREATE INDEX status_idx ON todolist (status);")
		cursor.execute("CREATE INDEX starid_idx ON todolist (starid);")
		conn.commit()

		# Change settings of SQLite file:
		cursor.execute("PRAGMA page_size=4096;")
		# Run a VACUUM of the table which will force a recreation of the
		# underlying "pages" of the file.
		# Please note that we are changing the "isolation_level" of the connection here,
		# but since we closing the conmnection just after, we are not changing it back
		conn.isolation_level = None
		cursor.execute("VACUUM;")

		# Close connection:
		cursor.close()

	logger.info("TODO done.")
