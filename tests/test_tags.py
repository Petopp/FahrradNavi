from fahrradnavi import tags as T


def wi(**kw):
    return T.classify_way({k.replace("__", ":"): v for k, v in kw.items()})


def test_excluded_ways():
    assert wi(highway="motorway") is None
    assert wi(highway="steps") is None
    assert wi(highway="footway") is None  # Fußweg ohne Radfreigabe
    assert wi(highway="cycleway", bicycle="no") is None
    assert wi(highway="residential", access="private") is None
    assert wi(highway="service", service="driveway") is None
    assert wi(highway="construction") is None


def test_footway_with_bicycle_yes_is_allowed_but_slow():
    w = wi(highway="footway", bicycle="yes")
    assert w is not None and w.hwc == T.HW_FOOTWAY


def test_oneway_variants():
    assert (wi(highway="residential", oneway="yes").fwd, wi(highway="residential", oneway="yes").bwd) == (True, False)
    w = wi(highway="residential", oneway="-1")
    assert (w.fwd, w.bwd) == (False, True)
    w = wi(highway="residential", oneway="yes", oneway__bicycle="no")
    assert (w.fwd, w.bwd) == (True, True)
    w = wi(highway="residential", oneway="yes", cycleway__left="opposite_lane")
    assert (w.fwd, w.bwd) == (True, True)
    w = wi(highway="tertiary", junction="roundabout")
    assert (w.fwd, w.bwd) == (True, False)


def test_infra():
    assert wi(highway="primary").infra == T.INFRA_NONE
    assert wi(highway="primary", cycleway__right="track").infra == T.INFRA_TRACK
    assert wi(highway="primary", cycleway="lane").infra == T.INFRA_LANE
    assert wi(highway="residential", bicycle_road="yes").infra == T.INFRA_BIKE_ROAD
    assert wi(highway="secondary", cycleway__both="shared_lane").infra == T.INFRA_SHARED


def test_surface_inference():
    assert wi(highway="cycleway").surface == T.SURF_SMOOTH
    assert wi(highway="track").surface == T.SURF_COMPACTED
    assert wi(highway="track", tracktype="grade4").surface == T.SURF_GRAVEL
    assert wi(highway="path", surface="cobblestone").surface == T.SURF_COBBLES
    assert wi(highway="path", surface="asphalt").surface == T.SURF_SMOOTH


def test_maxspeed():
    assert T.parse_maxspeed("30") == 30
    assert T.parse_maxspeed("50 km/h") == 50
    assert T.parse_maxspeed("DE:zone30") == 30
    assert T.parse_maxspeed("walk") == 7
    assert T.parse_maxspeed(None) == 0
    assert T.parse_maxspeed("none") == 0


def test_flags():
    assert wi(highway="tertiary", bicycle="use_sidepath").flags & T.FLAG_SIDEPATH
    assert wi(highway="cycleway", bridge="yes").flags & T.FLAG_BRIDGE
    assert wi(highway="cycleway", tunnel="yes").flags & T.FLAG_TUNNEL
