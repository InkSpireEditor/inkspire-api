<?php

namespace App\DataFixtures;

use Doctrine\Bundle\FixturesBundle\Fixture;
use Doctrine\Persistence\ObjectManager;
use Symfony\Component\PasswordHasher\Hasher\UserPasswordHasherInterface;
use App\Entity\User;
use App\Entity\File;
use App\Entity\Dir;
use App\Service\FilePathGenerator;
use App\Service\FileStorageServiceInterface;

class AppFixtures extends Fixture
{
    public function __construct(
        private UserPasswordHasherInterface $passwordHasher,
        private FilePathGenerator $filePathGenerator,
        private FileStorageServiceInterface $fileStorage,
    ) {
    }

    public function load(ObjectManager $manager): void
    {
        $email = $_ENV['FIXTURE_ADMIN_EMAIL'] ?? null;
        if ($email === null) {
            throw new \RuntimeException(
                'FIXTURE_ADMIN_EMAIL is not set. Add it to .env.local before loading fixtures.'
            );
        }

        $password = $_ENV['FIXTURE_ADMIN_PASSWORD'] ?? null;
        if ($password === null) {
            $password = bin2hex(random_bytes(8));
            echo sprintf("Fixture admin credentials — email: %s  password: %s\n", $email, $password);
        }

        // --- User ---
        $userRepository = $manager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $email]);

        if (!$user) {
            $user = new User();
            $user->setEmail($email);
            $user->setPassword($this->passwordHasher->hashPassword($user, $password));
            $manager->persist($user);
        }

        // --- Directory ---
        $dirRepository = $manager->getRepository(Dir::class);
        $dir = $dirRepository->findOneBy(['name' => 'Test Dir', 'user' => $user]);

        if (!$dir) {
            $dir = new Dir();
            $dir->setUser($user);
            $dir->setName('Test Dir');
            $dir->setSummary('This is a test directory.');
            $manager->persist($dir);
        }

        // --- Files ---
        // File paths include a unique suffix, so idempotency is handled at the name level.
        $fileRepository = $manager->getRepository(File::class);

        if (!$fileRepository->findOneBy(['name' => 'Loose File', 'user' => $user])) {
            $looseFile = new File();
            $looseFile->setUser($user);
            $looseFile->setName('Loose File');
            $looseFilePath = $this->filePathGenerator->generate('Loose File');
            $looseFile->setPath($looseFilePath);
            $this->fileStorage->write($looseFilePath, '# Loose File Content');
            $manager->persist($looseFile);
        }

        if (!$fileRepository->findOneBy(['name' => 'File in Dir', 'user' => $user])) {
            $fileInDir = new File();
            $fileInDir->setUser($user);
            $fileInDir->setName('File in Dir');
            $fileInDirPath = $this->filePathGenerator->generate('File in Dir');
            $fileInDir->setPath($fileInDirPath);
            $fileInDir->setDir($dir);
            $this->fileStorage->write($fileInDirPath, '# File in Dir Content');
            $manager->persist($fileInDir);
        }

        $manager->flush();
    }
}
