"""Captioned shop photos for the public "Our work" page.

Plates, old unit numbers and other businesses' branding were blurred and
location metadata stripped before these files were committed. Faces and
VINs are left out.
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
    GalleryPhoto('16-inside-glendale-auto-repair-shop.jpg', 'Inside our Glendale shop', 1200, 1600, 480, 640),
    GalleryPhoto('17-timing-chain-cam-timing.jpg', 'Timing chain job: setting cam timing', 1200, 1600, 480, 640),
    GalleryPhoto('18-ford-f550-shuttle-bus-ac.jpg', 'Ford F-550 shuttle bus in for A/C service', 1200, 1600, 480, 640),
    GalleryPhoto('19-dodge-charger-intake-manifold.jpg', 'Dodge Charger intake manifold replacement', 1200, 1600, 480, 640),
    GalleryPhoto('20-mercedes-benz-coupe-on-lift.jpg', 'Mercedes-Benz coupe up on the lift', 1200, 1600, 480, 640),
    GalleryPhoto('21-mobile-coolant-pressure-test-jaguar.jpg', 'Mobile coolant pressure test on a Jaguar S-Type', 1200, 1600, 480, 640),
    GalleryPhoto('22-ford-explorer-oil-in-coolant.jpg', 'Ford Explorer: oil in the coolant from a failed oil cooler', 1200, 1600, 480, 640),
    GalleryPhoto('23-ford-explorer-cooling-pressure-test.jpg', 'Ford Explorer cooling system pressure test', 1200, 1600, 480, 640),
    GalleryPhoto('24-maserati-ac-service.jpg', 'Maserati A/C service', 1200, 1600, 480, 640),
    GalleryPhoto('25-infiniti-q50-and-utv-in-shop.jpg', 'Infiniti Q50 and a UTV in the shop', 1200, 1600, 480, 640),
    GalleryPhoto('26-turbo-intake-manifold-removed.jpg', 'Turbo and intake manifold off for repair', 1200, 1600, 480, 640),
    GalleryPhoto('27-front-brake-rotor-replacement.jpg', 'Front brake rotor replacement', 1200, 1600, 480, 640),
    GalleryPhoto('28-front-brake-caliper-service.jpg', 'Front brake caliper and rotor service', 1200, 1600, 480, 640),
    GalleryPhoto('29-rear-brake-job.jpg', 'Rear brake job in progress', 1200, 1600, 480, 640),
    GalleryPhoto('30-new-engine-mount-installed.jpg', 'New engine mount installed', 1200, 1600, 480, 640),
    GalleryPhoto('31-under-car-engine-repair.jpg', 'Engine repair from underneath the car', 1200, 1600, 480, 640),
    GalleryPhoto('32-front-end-apart-engine-access.jpg', 'Front end apart for engine access', 1200, 1600, 480, 640),
    GalleryPhoto('33-mitsubishi-mirage-in-bay.jpg', 'Mitsubishi Mirage in the bay', 1200, 1600, 480, 640),
    GalleryPhoto('34-party-bus-seating-wiring.jpg', 'Party bus seating opened up for wiring repair', 1200, 1600, 480, 640),
    GalleryPhoto('35-mobile-repair-truck-road-call.jpg', 'Our mobile repair truck on a road call', 1600, 1200, 640, 480),
)

# Shown in the "Real work" strip on the home page, then linked to the full gallery.
HOME_PICKS: tuple[str, ...] = (
    '17-timing-chain-cam-timing.jpg',
    '14-ford-dps6-transmission-teardown.jpg',
    '06-ram-3500-dually-driveshaft.jpg',
    '21-mobile-coolant-pressure-test-jaguar.jpg',
)


def home_photos() -> list[GalleryPhoto]:
    by_file = {photo.file: photo for photo in PHOTOS}
    return [by_file[name] for name in HOME_PICKS if name in by_file]
