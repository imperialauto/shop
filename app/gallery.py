"""Captioned shop photos for the public "Our work" page.

Plates, old unit numbers and other businesses' branding were blurred and
location metadata stripped before these files were committed.
"""

from typing import NamedTuple


class GalleryPhoto(NamedTuple):
    file: str
    caption: str
    width: int
    height: int
    thumb_width: int
    thumb_height: int


PHOTOS: tuple[GalleryPhoto, ...] = (
    GalleryPhoto('01-glendale-auto-repair-shop-bay.jpg', 'Open bay at our Glendale shop', 1200, 1600, 480, 640),
    GalleryPhoto('02-ram-1500-outside-shop.jpg', 'Ram 1500 in for work outside our bays', 1200, 1600, 480, 640),
    GalleryPhoto('03-chevy-silverado-in-bay.jpg', 'Chevy Silverado pulled into the bay', 1200, 1600, 480, 640),
    GalleryPhoto('04-front-end-teardown.jpg', 'Front-end teardown in progress', 1600, 1200, 640, 480),
    GalleryPhoto('05-gmc-denali-in-shop.jpg', 'GMC Yukon Denali in the shop', 1200, 1600, 480, 640),
    GalleryPhoto('06-ram-3500-dually-driveshaft.jpg', 'Ram 3500 dually with the driveshaft out', 1600, 1200, 640, 480),
    GalleryPhoto('07-ram-ac-compressor.jpg', 'Ram 1500 A/C compressor job', 1200, 1600, 480, 640),
    GalleryPhoto('08-ac-vent-temperature-check.jpg', 'A/C vent temperature check after service', 1200, 1600, 480, 640),
    GalleryPhoto('09-chrysler-300-in-bay.jpg', 'Chrysler 300 in the bay', 1600, 1200, 640, 480),
    GalleryPhoto('10-ford-powerstroke-diesel.jpg', 'Ford Powerstroke diesel engine bay', 1200, 1600, 480, 640),
    GalleryPhoto('11-fuel-pressure-test.jpg', 'Fuel pressure testing', 1200, 1600, 480, 640),
    GalleryPhoto('12-engine-mount-work.jpg', 'Engine and mount work, front end opened up', 1200, 1600, 480, 640),
    GalleryPhoto('13-ford-f150-front-suspension.jpg', 'Ford F-150 front suspension work', 1200, 1600, 480, 640),
    GalleryPhoto('14-ford-dps6-transmission-teardown.jpg', 'Ford DPS6 PowerShift transmission torn down', 1200, 1600, 480, 640),
    GalleryPhoto('15-party-bus-interior-electrical.jpg', 'Party bus interior: lighting and electrical rebuild', 1600, 1200, 640, 480),
)
