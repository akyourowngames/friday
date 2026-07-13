import httpx
import pytest

from ares.watcher.fetchers.base import FetcherError, validate_target_url
from ares.watcher.fetchers.custom import CustomAPIFetcher, json_path_get
from ares.watcher.fetchers.instagram import InstagramFetcher
from ares.watcher.fetchers.website import WebsiteFetcher


@pytest.mark.asyncio
async def test_website_fetch_and_css_extraction():
    transport = httpx.MockTransport(lambda request: httpx.Response(200,html='<html><title>Shop</title><div id="price">$1,299.50</div><span class="stock">In stock</span></html>',request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await WebsiteFetcher(client).fetch("http://test.local/item", {"allow_private_network":True,"extractors":[{"field":"price","selector":"#price","type":"price"},{"field":"stock","selector":".stock","type":"text"}]})
    assert result.success is True
    assert result.content == {"price":1299.5,"stock":"In stock"}
    assert result.metadata["title"] == "Shop"


@pytest.mark.asyncio
async def test_website_default_text_strips_script_noise():
    transport = httpx.MockTransport(lambda request: httpx.Response(200,text='<script>noise</script><main>Hello <b>world</b></main>',request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await WebsiteFetcher(client).fetch("http://test.local", {"allow_private_network":True})
    assert result.content == "Hello\nworld"
    assert "noise" not in result.content


@pytest.mark.asyncio
async def test_fetcher_reports_http_error_without_raising():
    transport = httpx.MockTransport(lambda request: httpx.Response(503,text="down",request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await WebsiteFetcher(client).fetch("http://test.local", {"allow_private_network":True})
    assert result.success is False and result.status_code == 503


@pytest.mark.asyncio
async def test_custom_api_json_path_and_secret_url_redaction():
    transport = httpx.MockTransport(lambda request: httpx.Response(200,json={"data":{"items":[{"status":"up"}]}},request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await CustomAPIFetcher(client).fetch("http://test.local/x?api_key=secret", {"allow_private_network":True,"extractors":[{"field":"status","json_path":"$.data.items[0].status"}]})
    assert result.content == {"status":"up"}
    assert "secret" not in result.metadata["url"]
    assert json_path_get({"a":[1]}, "$.a[0]") == 1


@pytest.mark.asyncio
async def test_instagram_requires_explicit_graph_credentials():
    result = await InstagramFetcher().fetch("", {})
    assert result.success is False
    assert "access_token" in result.error


def test_url_safety_blocks_local_targets_by_default():
    with pytest.raises(FetcherError): validate_target_url("http://127.0.0.1/admin")
    assert validate_target_url("http://127.0.0.1/admin", allow_private_network=True).startswith("http")
    with pytest.raises(FetcherError): validate_target_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_blocked():
    def handler(request):
        return httpx.Response(302,headers={"location":"http://other.local/private"},request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result=await WebsiteFetcher(client).fetch("http://test.local",{"allow_private_network":True})
    assert result.success is False and "Cross-origin redirect" in result.error
