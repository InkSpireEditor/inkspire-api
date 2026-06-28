<?php

namespace App\Service;

use Symfony\Contracts\HttpClient\Exception\TransportExceptionInterface;
use Symfony\Contracts\HttpClient\HttpClientInterface;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Contracts\Cache\CacheInterface;
use Symfony\Contracts\Cache\ItemInterface;
use Twig\Environment;

class OllamaService implements OllamaServiceInterface
{
    private string $ollamaServiceUrl;
    private ?int $temperature;
    private ?int $numCtx;
    private ?int $topK;
    private ?float $topP;
    private const CACHE_KEY_MODELS = 'ollama_available_models';
    private const CACHE_TTL_MODELS = 3600; // Cache for 1 hour (3600 seconds)

    public function __construct(
        private readonly HttpClientInterface $client,
        private readonly CacheInterface $cache,
        private readonly Environment $twig,
        string $ollamaServiceUrl,
        ?int $temperature,
        ?int $numCtx,
        ?int $topK,
        ?float $topP
    ) {
        $this->ollamaServiceUrl = $ollamaServiceUrl;
        $this->temperature = $temperature;
        $this->numCtx = $numCtx;
        $this->topK = $topK;
        $this->topP = $topP;
    }

    /**
     * Fetches the list of available models from the Ollama API via the Python backend,
     * utilizing a time-based cache.
     *
     * @return array The list of available models.
     * @throws \Symfony\Contracts\HttpClient\Exception\ExceptionInterface
     */
    public function getAvailableModels(): array
    {
        return $this->cache->get(self::CACHE_KEY_MODELS, function (ItemInterface $item) {
            $item->expiresAfter(self::CACHE_TTL_MODELS);

            try {
                $response = $this->client->request('GET', $this->ollamaServiceUrl . '/api/tags', [
                    'timeout' => 5,
                    'max_duration' => 10,
                ]);
            } catch (TransportExceptionInterface) {
                return [];
            }

            if ($response->getStatusCode() !== Response::HTTP_OK) {
                return [];
            }

            $data = $response->toArray();
            $modelsData = $data['models'] ?? $data;

            $modelNames = [];
            foreach ($modelsData as $model) {
                if (isset($model['name'])) {
                    $modelNames[] = ['name' => $model['name']];
                }
            }

            return $modelNames;
        });
    }

    /**
     * Generates text using a specified model and prompt from the Ollama API.
     *
     * @param string $model The name of the model to use.
     * @param string $prompt The user's prompt.
     * @return string The generated text response.
     * @throws \Symfony\Contracts\HttpClient\Exception\ExceptionInterface
     */
    public function generateText(string $model, string $prompt): string
    {
        $finalPrompt = $this->twig->render('prompt.twig', [
            'prompt' => $prompt,
        ]);

        $options = [
            'temperature' => $this->temperature ?? 1,
            'num_ctx' => $this->numCtx,
            'top_k' => $this->topK,
            'top_p' => $this->topP,
        ];

        // Filter out null values
        $options = array_filter($options, fn ($value) => !is_null($value));

        try {
            $response = $this->client->request('POST', $this->ollamaServiceUrl . '/api/generate', [
                'json' => [
                    'model' => $model,
                    'prompt' => $finalPrompt,
                    'stream' => false,
                    'options' => $options,
                ],
                'timeout' => 5,
                'max_duration' => 60,
            ]);
        } catch (TransportExceptionInterface $e) {
            throw new \RuntimeException('Ollama service is unreachable: ' . $e->getMessage(), 0, $e);
        }

        if ($response->getStatusCode() !== Response::HTTP_OK) {
            throw new \RuntimeException('Failed to generate text from Ollama API.');
        }

        $data = $response->toArray();
        return  $data['response'] ?? '';
    }
}
