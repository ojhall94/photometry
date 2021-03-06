#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Create catalogs of stars in a given TESS observing sector.

.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
"""

from __future__ import division, with_statement, print_function, absolute_import
import numpy as np
import os
import sqlite3
import logging
import itertools
import contextlib
from .tasoc_db import TASOC_DB
from .utilities import (add_proper_motion, load_settings, find_catalog_files,
						radec_to_cartesian, cartesian_to_radec)

#------------------------------------------------------------------------------
def make_catalog(sector, input_folder=None, cameras=None, ccds=None, coord_buffer=0.2, overwrite=False):
	"""
	Create catalogs of stars in a given TESS observing sector.

	Parameters:
		sector (integer): TESS observing sector.
		input_folder (string or None, optional):  Input folder to create catalog file in.
			If ``None``, the input directory in the environment variable ``TESSPHOT_INPUT`` is used.
		cameras (iterable or None, optional): TESS cameras (1-4) to create catalogs for. If ``None`` all cameras are created.
		ccds (iterable or None, optional): TESS ccds (1-4) to create catalogs for. If ``None`` all ccds are created.
		coord_buffer (float, optional): Buffer in degrees around each CCD to include in catalogs. Default=0.1.
		overwrite (boolean, optional): Overwrite existing catalogs. Default=``False``.

	Note:
		This function requires the user to be connected to the TASOC network
		at Aarhus University. It connects to the TASOC database to get a complete
		list of all stars in the TESS Input Catalog (TIC), which is a very large
		table.

	.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
	"""

	logger = logging.getLogger(__name__)

	# Make sure cameras and ccds are iterable:
	cameras = (1, 2, 3, 4) if cameras is None else (cameras, )
	ccds = (1, 2, 3, 4) if ccds is None else (ccds, )

	settings = load_settings(sector=sector)
	sector_reference_time = settings['reference_time']
	epoch = (sector_reference_time - 2451544.5)/365.25

	if input_folder is None:
		input_folder = os.environ.get('TESSPHOT_INPUT', os.path.join(os.path.dirname(__file__), 'tests', 'input'))
	logger.info("Saving results to '%s'", input_folder)

	# Open connection to the central TASOC database.
	# This requires that users are on the TASOC network at Aarhus University.
	with TASOC_DB() as tasocdb:
		# Loop through the cameras and CCDs that should have catalogs created:
		for camera, ccd in itertools.product(cameras, ccds):

			logger.info("Running SECTOR=%s, CAMERA=%s, CCD=%s", sector, camera, ccd)

			# Create SQLite file:
			# TODO: Could we use "find_catalog_files" instead?
			catalog_file = os.path.join(input_folder, 'catalog_sector{0:03d}_camera{1:d}_ccd{2:d}.sqlite'.format(sector, camera, ccd))
			if os.path.exists(catalog_file):
				if overwrite:
					os.remove(catalog_file[0])
				else:
					logger.info("Already done")
					continue

			with contextlib.closing(sqlite3.connect(catalog_file)) as conn:
				conn.row_factory = sqlite3.Row
				cursor = conn.cursor()

				# Table which stores information used to generate catalog:
				cursor.execute("""CREATE TABLE settings (
					sector INT NOT NULL,
					camera INT NOT NULL,
					ccd INT NOT NULL,
					ticver INT NOT NULL,
					reference_time DOUBLE PRECISION NOT NULL,
					epoch DOUBLE PRECISION NOT NULL,
					coord_buffer DOUBLE PRECISION NOT NULL,
					camera_centre_ra DOUBLE PRECISION NOT NULL,
					camera_centre_dec DOUBLE PRECISION NOT NULL,
					footprint TEXT NOT NULL
				);""")

				cursor.execute("""CREATE TABLE catalog (
					starid BIGINT PRIMARY KEY NOT NULL,
					ra DOUBLE PRECISION NOT NULL,
					decl DOUBLE PRECISION NOT NULL,
					ra_J2000 DOUBLE PRECISION NOT NULL,
					decl_J2000 DOUBLE PRECISION NOT NULL,
					pm_ra REAL,
					pm_decl REAL,
					tmag REAL NOT NULL,
					teff REAL
				);""")

				# Get the footprint on the sky of this sector:
				tasocdb.cursor.execute("SELECT footprint,camera_centre_ra,camera_centre_dec FROM tasoc.pointings WHERE sector=%s AND camera=%s AND ccd=%s;", (
					sector,
					camera,
					ccd
				))
				row = tasocdb.cursor.fetchone()
				if row is None:
					raise IOError("The given sector, camera, ccd combination was not found in TASOC database: (%s,%s,%s)", sector, camera, ccd)
				footprint = row[0]
				camera_centre_ra = row[1]
				camera_centre_dec = row[2]

				# Transform footprint into numpy array:
				a = footprint[2:-2].split('),(')
				a = np.array([b.split(',') for b in a], dtype='float64')

				# If we need to, we should add a buffer around the footprint,
				# by extending all points out from the centre with a set amount:
				# We are cheating a little bit here with the spherical geometry,
				# but it shouldn't matter too much.
				if coord_buffer > 0:
					# Convert ra-dec to cartesian coordinates:
					a_xyz = radec_to_cartesian(a)

					# Find center of footprint:
					origin_xyz = np.mean(a_xyz, axis=0)
					origin_xyz /= np.linalg.norm(origin_xyz)

					# Just for debugging:
					origin = cartesian_to_radec(origin_xyz).flatten()
					logger.debug("Centre of CCD: (%f, %f)", origin[0], origin[1])

					# Add buffer zone, by expanding polygon away from origin:
					for k in range(a.shape[0]):
						vec = a_xyz[k,:] - origin_xyz
						uvec = vec/np.linalg.norm(vec)
						a_xyz[k,:] += uvec*np.radians(coord_buffer)
						a_xyz[k,:] /= np.linalg.norm(a_xyz[k,:])
					a_xyz = np.clip(a_xyz, -1, 1)

					# Convert back to ra-dec coordinates:
					a = cartesian_to_radec(a_xyz)

				# Make footprint into string that will be understood by database:
				footprint = '{' + ",".join([str(s) for s in a.flatten()]) + '}'
				logger.info(footprint)

				# Save settings to SQLite:
				cursor.execute("INSERT INTO settings (sector,camera,ccd,reference_time,epoch,coord_buffer,footprint,camera_centre_ra,camera_centre_dec,ticver) VALUES (?,?,?,?,?,?,?,?,?,?);", (
					sector,
					camera,
					ccd,
					sector_reference_time,
					epoch + 2000.0,
					coord_buffer,
					footprint,
					camera_centre_ra,
					camera_centre_dec,
					7 # TODO: TIC Version hard-coded to TIC-7. This should obviously be changed when TIC is updated
				))
				conn.commit()

				# Query the TESS Input Catalog table for all stars in the footprint.
				# This is a MASSIVE table, so this query may take a while.
				tasocdb.cursor.execute("SELECT starid,ra,decl,pm_ra,pm_decl,\"Tmag\",\"Teff\",version FROM tasoc.tic_newest WHERE q3c_poly_query(ra, decl, %s) AND disposition IS NULL;", (
					footprint,
				))

				# We need a list of when the sectors are in time:
				logger.info('Projecting catalog {0:.3f} years relative to 2000'.format(epoch))

				for row in tasocdb.cursor.fetchall():
					# Add the proper motion to each coordinate:
					if row['pm_ra'] and row['pm_decl']:
						ra, dec = add_proper_motion(row['ra'], row['decl'], row['pm_ra'], row['pm_decl'], sector_reference_time, epoch=2000.0)
						logger.debug("(%f, %f) => (%f, %f)", row[1], row[2], ra, dec)
					else:
						ra = row['ra']
						dec = row['decl']

					# Save the coordinates in SQLite database:
					cursor.execute("INSERT INTO catalog (starid,ra,decl,ra_J2000,decl_J2000,pm_ra,pm_decl,tmag,teff) VALUES (?,?,?,?,?,?,?,?,?);", (
						int(row['starid']),
						ra,
						dec,
						row['ra'],
						row['decl'],
						row['pm_ra'],
						row['pm_decl'],
						row['Tmag'],
						row['Teff']
					))

				cursor.execute("CREATE UNIQUE INDEX starid_idx ON catalog (starid);")
				cursor.execute("CREATE INDEX ra_dec_idx ON catalog (ra, decl);")
				conn.commit()

				# Change settings of SQLite file:
				cursor.execute("PRAGMA page_size=4096;")
				# Run a VACUUM of the table which will force a recreation of the
				# underlying "pages" of the file.
				# Please note that we are changing the "isolation_level" of the connection here,
				# but since we closing the connnection just after, we are not changing it back
				conn.isolation_level = None
				cursor.execute("VACUUM;")

				cursor.close()

		logger.info("Catalog done.")

	logger.info("All catalogs done.")
