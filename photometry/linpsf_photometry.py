#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Created on Thu Jan 18 14:08:36 2018

@author: Jonas Svenstrup Hansen <jonas.svenstrup@gmail.com>
"""

from __future__ import division, with_statement, print_function, absolute_import
import numpy as np
import scipy
import matplotlib.pyplot as plt
import logging
import os
from .BasePhotometry import BasePhotometry, STATUS
from .psf import PSF
from .utilities import mag2flux
from .plots import plot_image_fit_residuals, save_figure
from .residual_mask import four_pixel_mask

class LinPSFPhotometry(BasePhotometry):

	def __init__(self, *args, **kwargs):
		"""
		Linear PSF photometry.

		Do point spread function photometry with fixed centroids. The flux of
		all stars in the image are fitted simultaneously using a linear least
		squares method.


		Note:
			Inspired by the :py:class:`psf_photometry` class set up by
			Rasmus Handberg <rasmush@phys.au.dk>. The code in this
			:py:func:`__init__` function as well as the logging, catalog call,
			time domain loop structure, catalog star limits and lightcurve
			output is copied from that class.

		.. code author:: Jonas Svenstrup Hansen <jonas.svenstrup@gmail.com>
		"""
		# Call the parent initializing:
		# This will set several default settings
		super(self.__class__, self).__init__(*args, **kwargs)

		# Create instance of the PSF for the given pixel stamp:
		# NOTE: If we run resize_stamp at any point in the code,
		#       we should also update self.PSF.
		# TODO: Maybe we should move this into BasePhotometry?
		self.psf = PSF(self.camera, self.ccd, self.stamp)

	def do_photometry(self):
		"""Linear PSF Photometry
		TODO: add description of method and what A and b are
		"""

		logger = logging.getLogger(__name__)

		# Load catalog to determine what stars to fit:
		cat = self.catalog
		staridx = np.squeeze(np.where(cat['starid']==self.starid))

		# Log full catalog for current stamp:
		logger.debug(cat)

		# Calculate distance from main target:
		cat['dist'] = np.sqrt((cat['row_stamp'][staridx] - cat['row_stamp'])**2 + \
						(cat['column_stamp'][staridx] - cat['column_stamp'])**2)

		# Find indices of stars in catalog to fit:
		# (only include stars that are close to the main target and that are
		# not much fainter)
		indx = (cat['dist'] < 5) & (cat['tmag'][staridx]-cat['tmag'] > -5)
		nstars = np.sum(indx)

		# Get target star index in the reduced catalog of stars to fit:
		staridx = np.squeeze(np.where(cat[indx]['starid']==self.starid))
		logger.debug('Target star index: %s', np.str(staridx))

		# Preallocate flux sum array for contamination calculation:
		fluxes_sum = np.zeros(nstars)

		# Start looping through the images (time domain):
		for k, img in enumerate(self.images):
			# Get catalog at current time in MJD:
			cat = self.catalog_attime(self.lightcurve['time'][k])

			# Reduce catalog to only include stars that should be fitted:
			cat = cat[indx]

			# Log reduced catalog for the stamp at the current time:
			logger.debug(cat)

			# Get the number of pixels in the image:
			npx = img.size

			# Create A, the 2D of vertically reshaped PRF 1D arrays:
			A = np.empty([npx, nstars])

			# Preallocate target row and col position:
			for col,target in enumerate(cat):
				# Get star parameters with flux set to 1 and reshape:
				params0 = np.array(
						[target['row_stamp'], target['column_stamp'], 1.]
						).reshape(1, 3)
				# Write
				if col == staridx:
					target_row = params0[0]
					target_col = params0[1]

				# Fill out column of A with reshaped PRF array from one star:
				A[:,col] = np.reshape(self.psf.integrate_to_image(params0,
										cutoff_radius=20), npx)

			# Crate b, the solution array by reshaping the image to a 1D array:
			b = np.reshape(img, npx)

			# Do linear least squares fit to solve Ax=b:
			try:
				# Linear least squares:
				res = np.linalg.lstsq(A,b)
				fluxes = res[0]

				# Non-negative linear least squares:
#				fluxes, rnorm = scipy.optimize.nnls(A,b)
#				res = 'notfailed'
			except:
				res = 'failed'
			logger.debug('Result of linear psf photometry: ' + np.str(res))

			# Pass result if fit did not fail:
			if res is not 'failed':
				# Get flux of target star:
				result = fluxes[staridx]

				logger.debug('PSF fitted fluxes are: ' + np.str(fluxes))
				logger.debug('PSF fitted result is: ' + np.str(result))

				# Generate fitted and residual images from A and fitted fluxes:
				img_fit = np.reshape(np.sum(A*fluxes, 1), img.shape)
				img_res = img - img_fit

				# Get indices of mask in residual image:
				res_mask = four_pixel_mask(target_row, target_col)
				logger.debug('Indices of residual mask, 2D: ' + np.array_str(res_mask))
				res_mask = np.ravel_multi_index(res_mask, dims=img.shape)
				logger.debug('Indices of residual mask, ravelled: ' + np.array_str(res_mask))

				# Do aperture photometry on residual image:
				res_mask_sum = np.sum(img_res.ravel()[res_mask])
				logger.debug('Residual aperture photometry result: ' + np.str(res_mask_sum))

				# Add residual photometry result to target flux value:
				result += res_mask_sum

				# Add the result of the main star to the lightcurve:
				self.lightcurve['flux'][k] = result
				self.lightcurve['pos_centroid'][k] = [np.NaN, np.NaN]
				self.lightcurve['quality'][k] = 0

				# Add current fitted fluxes for contamination calculation:
				fluxes_sum += fluxes

				if self.plot:
					# Make plot for debugging:
					fig = plt.figure()

					# Add subplots with the image, fit and residuals:
					ax_list = plot_image_fit_residuals(fig=fig,
							image=img,
							fit=img_fit,
							residuals=img_res)

					# Set subplot titles:
					title_list = ['Simulated image', 'Least squares PSF fit', 'Residual image']
					for ax, title in zip(ax_list, title_list):
						# Add title to subplot:
						ax.set_title(title)

						# Add star position to subplot:
						# TODO: get target star position from somewhere else than result4plot which is to be outphased
						ax_list[0].scatter(target_col, target_row, c='r', alpha=0.5)

					# Save figure to file:
					fig_name = 'tess_{0:09d}'.format(self.starid) + '_linpsf_{0:09d}'.format(k)
					save_figure(os.path.join(self.plot_folder, fig_name))

			# Pass result if fit failed:
			else:
				logger.warning("We should flag that this has not gone well.")

				self.lightcurve['flux'][k] = np.NaN
				self.lightcurve['pos_centroid'][k] = [np.NaN, np.NaN]
				self.lightcurve['quality'][k] = 1 # FIXME: Use the real flag!


		if np.sum(np.isnan(self.lightcurve['flux'])) == len(self.lightcurve['flux']):
			# Set contamination to NaN if all flux values are NaN:
			self.report_details(error='All target flux values are NaN.')
			return STATUS.ERROR
		else:
			# Divide by number of added fluxes to get the mean flux:
			fluxes_mean =  fluxes_sum / np.sum(~np.isnan(self.lightcurve['flux']))
			logger.debug('Mean fluxes are: '+np.str(fluxes_mean))

			# Calculate contamination from other stars in target PSF using latest A:
			not_target_star = np.arange(len(fluxes_mean))!=staridx
			contamination = \
				np.sum(A[:,not_target_star].dot(fluxes_mean[not_target_star]) * A[:,staridx]) \
				/fluxes_mean[staridx]

			logger.info("Contamination: %f", contamination)
			self.additional_headers['AP_CONT'] = (contamination, 'AP contamination')

			# If contamination is high, return a warning:
			if contamination > 0.1:
				self.report_details(error='High contamination')
				return STATUS.WARNING


		# Return whether you think it went well:
		return STATUS.OK
