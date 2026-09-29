import warnings

# starlette's TestClient warns that it will move to httpx2; harmless for these tests.
warnings.filterwarnings("ignore", message=".*httpx2.*")
