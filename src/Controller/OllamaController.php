<?php

namespace App\Controller;

use App\Entity\User;
use App\Service\FileStorageServiceInterface;
use App\Service\OllamaServiceInterface;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\DependencyInjection\Attribute\Autowire;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\RateLimiter\RateLimiterFactory;
use Symfony\Component\Routing\Attribute\Route;
use Symfony\Component\Security\Http\Attribute\CurrentUser;
use Symfony\Component\Security\Http\Attribute\IsGranted;

#[IsGranted('IS_AUTHENTICATED_FULLY')]
#[Route('/api/ollama', name: 'app_api_ollama_')]
class OllamaController extends AbstractController
{
    public function __construct(private readonly FileStorageServiceInterface $fileStorage)
    {
    }

    /**
     * Lists available Ollama models.
     */
    #[Route('/models', name: 'models_list', methods: ['GET'])]
    public function listModels(#[CurrentUser] User $user, OllamaServiceInterface $ollamaService): Response
    {
        try {
            $models = $ollamaService->getAvailableModels();
            return $this->json($models);
        } catch (\Exception $e) {
            return $this->json([
                'message' => 'Could not retrieve models from the Ollama service.',
                'error' => $e->getMessage(),
            ], Response::HTTP_INTERNAL_SERVER_ERROR);
        }
    }

    /**
     * Generates text using a specified model and prompt, and persists it to the file.
     */
    #[Route('/generate', name: 'generate', methods: ['POST'])]
    public function generate(
        Request $request,
        #[CurrentUser] User $user,
        OllamaServiceInterface $ollamaService,
        \App\Repository\FileRepository $fileRepository,
        #[Autowire(service: 'limiter.ollama_generate')] RateLimiterFactory $ollamaGenerateLimiter
    ): Response {
        $limiter = $ollamaGenerateLimiter->create($user->getUserIdentifier());
        if (!$limiter->consume()->isAccepted()) {
            return $this->json(['message' => 'Too many requests'], Response::HTTP_TOO_MANY_REQUESTS);
        }

        $data = $request->toArray();
        $fileId = $data['id'] ?? null;
        $model = $data['model'] ?? null;
        $prompt = $data['prompt'] ?? null;

        if (!$fileId || !$model || !$prompt) {
            return $this->json(['message' => 'Missing "id", "model" or "prompt" in request body'], Response::HTTP_BAD_REQUEST);
        }

        if (!is_string($model) || mb_strlen($model) > 255) {
            return $this->json(['message' => 'Invalid model name'], Response::HTTP_UNPROCESSABLE_ENTITY);
        }
        if (!is_string($prompt) || mb_strlen($prompt) > 10000) {
            return $this->json(['message' => 'Prompt too long (max 10000 characters)'], Response::HTTP_UNPROCESSABLE_ENTITY);
        }

        $file = $fileRepository->find($fileId);
        if (!$file || $file->getUser() !== $user) {
            return $this->json(['message' => 'File not found or access denied'], Response::HTTP_FORBIDDEN);
        }

        // Storage checks are intentionally outside the Ollama try/catch so path
        // validation errors and missing-file errors are not swallowed by it.
        $path = $file->getPath();
        if (!$this->fileStorage->exists($path)) {
            return $this->json(['message' => 'File not found on disk'], Response::HTTP_NOT_FOUND);
        }
        $currentContent = $this->fileStorage->read($path);

        try {
            $generatedText = $ollamaService->generateText($model, $prompt);
        } catch (\Exception $e) {
            return $this->json(
                ['message' => 'An error occurred while communicating with the Ollama service: ' . $e->getMessage()],
                Response::HTTP_INTERNAL_SERVER_ERROR
            );
        }

        // Persisting to file
        $this->fileStorage->write($path, $currentContent . $generatedText);

        return $this->json([
            'snippet' => $generatedText
        ]);
    }
}
