from backend.src.main import _hpp_data_rows, _hpp_site_devices


def test_hpp_site_search_rows_are_normalized():
    payload = {
        "data": {
            "page": 1,
            "pageSize": 100,
            "rows": [
                {"siteId": "hpp-site-1", "siteName": "Customer Store"},
                {"siteId": "hpp-site-2", "siteName": "Second Store"},
            ],
            "total": 2,
            "totalPage": 1,
        },
        "errorCode": "0",
    }

    sites = _hpp_data_rows(payload, "rows", "list", "sites", "items")

    assert [site["siteId"] for site in sites] == ["hpp-site-1", "hpp-site-2"]


def test_hpp_site_search_legacy_list_shape_remains_supported():
    payload = {"data": {"list": [{"siteId": "legacy-site"}]}, "errorCode": "0"}

    assert _hpp_data_rows(payload, "rows", "list", "sites") == [{"siteId": "legacy-site"}]


def test_hpp_device_rows_are_normalized():
    payload = {
        "data": {
            "rows": [
                {"deviceSerial": "ABC123"},
                {"deviceSerial": "XYZ789"},
                None,
            ]
        },
        "errorCode": "0",
    }

    devices = _hpp_site_devices(payload)

    assert [device["deviceSerial"] for device in devices] == ["ABC123", "XYZ789"]
