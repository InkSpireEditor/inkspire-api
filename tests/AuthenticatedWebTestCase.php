<?php

namespace App\Tests;

use App\Entity\User;
use App\Service\FilePathGenerator;
use Doctrine\ORM\EntityManagerInterface;
use Symfony\Bundle\FrameworkBundle\KernelBrowser;
use Symfony\Bundle\FrameworkBundle\Test\WebTestCase;

abstract class AuthenticatedWebTestCase extends WebTestCase
{
    protected KernelBrowser $client;
    protected ?EntityManagerInterface $entityManager;
    protected FilePathGenerator $filePathGenerator;

    protected string $email = 'test@example.com';
    protected string $password = 'password';

    protected ?string $token = null;

    /**
     * Resolves a stored filename (relative) to its absolute path in the test storage directory.
     * Use this whenever a test needs to call file_put_contents(), touch(), assertFileExists(), etc.
     */
    protected function resolveFilePath(string $filename): string
    {
        $container = static::getContainer();
        return $container->getParameter('kernel.project_dir')
            . '/' . $container->getParameter('app.files_dir')
            . '/' . $filename;
    }

    protected function deauthenticateClient(): void
    {
        $this->client->setServerParameter('HTTP_Authorization', '');
        // Also drop the jwt_token cookie the client kept from login, so the
        // client carries no credentials by any transport.
        $this->client->getCookieJar()->clear();
    }

    protected function authenticateClient(): void
    {
        $this->client->setServerParameter('HTTP_Authorization', 'Bearer ' . $this->token);
    }

    protected function createAuthenticatedClient($client, string $email, string $password)
    {
        $client->jsonRequest('POST', '/auth', [
            'username' => $email,
            'password' => $password,
        ]);

        $data = json_decode($client->getResponse()->getContent(), true);
        $this->token = $data['token'];
        $this->authenticateClient();

        return $client;
    }

    protected function createUser(string $email, string $password): User
    {
        $container = static::getContainer();
        $passwordHasher = $container->get('security.user_password_hasher');
        $this->filePathGenerator = $container->get(FilePathGenerator::class);

        $user = (new User())->setEmail($email);
        $user->setPassword($passwordHasher->hashPassword($user, $password));

        return $user;
    }

    protected function setUp(): void
    {
        parent::setUp();
        $this->client = static::createClient();
        $container = static::getContainer();
        $this->entityManager = $container->get('doctrine.orm.entity_manager');
        $this->filePathGenerator = $container->get(FilePathGenerator::class);

        // Ensure the test files directory exists
        $projectRoot = $container->getParameter('kernel.project_dir');
        $filesDir = $projectRoot . '/' . $container->getParameter('app.files_dir');
        if (!is_dir($filesDir)) {
            mkdir($filesDir, 0777, true);
        }

        // Clean up the database before each test
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\File')->execute();
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\Dir')->execute();
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\RefreshToken')->execute();
        $this->entityManager->createQuery('DELETE FROM App\\Entity\\User')->execute();

        $user = $this->createUser($this->email, $this->password);
        $this->entityManager->persist($user);
        $this->entityManager->flush();
        $this->client = $this->createAuthenticatedClient($this->client, $this->email, $this->password);
    }


    protected function tearDown(): void
    {
        $container = static::getContainer();
        $projectRoot = $container->getParameter('kernel.project_dir');
        $filesDir = $projectRoot . '/' . $container->getParameter('app.files_dir');
        
        if (is_dir($filesDir)) {
            // Delete all files in the test storage directory, not just *.ink, so
            // files with any extension created during a test are always cleaned up.
            foreach (glob($filesDir . '/*') as $file) {
                if (is_file($file)) {
                    unlink($file);
                }
            }
        }

        parent::tearDown();

        // doing this is recommended to avoid memory leaks
        $this->entityManager->close();
        $this->entityManager = null;
    }
}
