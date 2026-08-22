<?php

namespace App\Controller;

use App\Entity\User;
use App\Service\FileStorageServiceInterface;
use App\Service\LLMServiceInterface;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\DependencyInjection\Attribute\Autowire;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\RateLimiter\RateLimiterFactory;
use Symfony\Component\Routing\Attribute\Route;
use Symfony\Component\Security\Http\Attribute\CurrentUser;
use Symfony\Component\Security\Http\Attribute\IsGranted;

/**
 * Controller for the LLM-backed text generation endpoints.
 */
#[IsGranted('IS_AUTHENTICATED_FULLY')]
#[Route('/api/llm', name: 'app_api_llm_')]
class LLMController extends AbstractController
{
    /** Upper bound on a model identifier, across every configured provider. */
    private const MAX_MODEL_NAME_LENGTH = 255;

    /**
     * Upper bound on a single generation prompt. Kept well under the smallest
     * context window we expect a provider to offer, so the rendered template
     * still fits alongside it.
     */
    private const MAX_PROMPT_LENGTH = 10000;

    public function __construct(private readonly FileStorageServiceInterface $fileStorage) {}

    /**
     * Lists available models across all configured LLM providers.
     */
    #[Route('/models', name: 'models_list', methods: ['GET'])]
    public function listModels(#[CurrentUser] User $user, LLMServiceInterface $llmService): Response
    {
        try {
            $models = $llmService->getAvailableModels();
            return $this->json($models);
        } catch (\Exception $e) {
            return $this->json([
                'message' => 'Could not retrieve models from the LLM providers.',
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
        #[CurrentUser]
        User $user,
        LLMServiceInterface $llmService,
        \App\Repository\FileRepository $fileRepository,
        #[Autowire(service: 'limiter.llm_generate')]
        RateLimiterFactory $llmGenerateLimiter
    ): Response {
        $limiter = $llmGenerateLimiter->create($user->getUserIdentifier());
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

        if (!is_string($model) || mb_strlen($model) > self::MAX_MODEL_NAME_LENGTH) {
            return $this->json(['message' => 'Invalid model name'], Response::HTTP_UNPROCESSABLE_ENTITY);
        }
        if (!is_string($prompt) || mb_strlen($prompt) > self::MAX_PROMPT_LENGTH) {
            return $this->json(
                ['message' => sprintf('Prompt too long (max %d characters)', self::MAX_PROMPT_LENGTH)],
                Response::HTTP_UNPROCESSABLE_ENTITY
            );
        }

        $file = $fileRepository->find($fileId);
        if (!$file || $file->getUser() !== $user) {
            return $this->json(['message' => 'File not found or access denied'], Response::HTTP_FORBIDDEN);
        }

        // Storage checks are intentionally outside the LLM try/catch so path
        // validation errors and missing-file errors are not swallowed by it.
        $path = $file->getPath();
        if (!$this->fileStorage->exists($path)) {
            return $this->json(['message' => 'File not found on disk'], Response::HTTP_NOT_FOUND);
        }
        $currentContent = $this->fileStorage->read($path);

        try {
            $generatedText = $llmService->generateText($model, $prompt);
        } catch (\Exception $e) {
            return $this->json(
                ['message' => 'An error occurred while communicating with the LLM provider: ' . $e->getMessage()],
                Response::HTTP_INTERNAL_SERVER_ERROR
            );
        }

        // Persisting to file
        $this->fileStorage->write($path, $currentContent . $generatedText);

        return $this->json([
            'snippet' => $generatedText,
        ]);
    }
}
