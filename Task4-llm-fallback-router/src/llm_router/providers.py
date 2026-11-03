from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Protocol
import httpx

class ProviderConnectionError(Exception):
    pass

@dataclass
class ProviderRequest:
    messages: List[Dict[str, Any]]
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None

@dataclass
class ProviderResult:
    status: int
    body: Optional[Dict[str, Any]]
    retry_after_s: Optional[float] = None

class Provider(Protocol):
    name: str
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        ...

class HttpProvider:
    def __init__(self, name: str, base_url: str, api_key: str, model: str, client: Optional[httpx.AsyncClient] = None):
        self.name = name
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.client = client

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        payload = {
            'model': request.model or self.model,
            'messages': request.messages,
        }
        if request.max_tokens is not None:
            payload['max_tokens'] = request.max_tokens
        if request.temperature is not None:
            payload['temperature'] = request.temperature
        if request.extra:
            payload.update(request.extra)

        headers = {
            'Content-Type': 'application/json'
        }
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}' 

        url = self.base_url
        if not url.endswith('/chat/completions'):
            if url.endswith('/v1'):
                url = f'{url}/chat/completions'
            else:
                url = f'{url}/v1/chat/completions'

        close_client = False
        client = self.client
        if client is None:
            client = httpx.AsyncClient(timeout=None)
            close_client = True

        try:
            resp = await client.post(url, json=payload, headers=headers)
            body = None
            try:
                body = resp.json()
            except Exception:
                body = None

            retry_after = None
            if 'retry-after' in resp.headers:
                try:
                    retry_after = float(resp.headers['retry-after'])
                except ValueError:
                    retry_after = None

            return ProviderResult(
                status=resp.status_code,
                body=body,
                retry_after_s=retry_after
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as e:
            raise ProviderConnectionError(str(e)) from e
        finally:
            if close_client:
                await client.aclose()
